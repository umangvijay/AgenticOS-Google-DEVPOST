"""Command reconciler (Phase 4 §4.18) — the first bounded self-healing loop.

Observe durable state → identify a violated invariant → perform an
authorized conditional repair → verify the resulting state → record the
outcome. Bounded batches, idempotent repairs, explicit repair reasons, no
deletion of evidence, no blind replay of uncertain external effects.

Detected conditions (per §4.18 table) and repairs:
- accepted/queued command past its deadline        → EXPIRED (conditional)
- cancel_requested with no acknowledgement aging   → escalate (metric/log)
- outbox backlog / abandoned leases                → dispatcher pass (§4.8)
- completed child with unresolved parent wait      → resolve + wake (§4.13)
- unpublished command.accepted for a command with
  no run yet (crash between accept and run creation) → re-drive via the
  outbox (the consumer's create_if_absent dedups; no duplicate logical run)
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from datetime import datetime, timezone
from backend.models.admission import epoch_now
from backend.models.command import CommandState

logger = logging.getLogger(__name__)

METRICS: Dict[str, int] = {}


def reconciler_metrics() -> Dict[str, int]:
    return dict(METRICS)


async def reconcile_once(command_repo, wait_repo=None, workflow_repo=None,
                         schedule_repo=None, message_bus=None,
                         batch: int = 50, mcp_repo=None) -> Dict[str, int]:
    """One bounded reconciliation pass. Returns per-repair counts."""
    from backend.services.outbox_dispatcher import run_dispatcher
    report = {"expired": 0, "cancellation_escalations": 0,
              "outbox_published": 0, "parent_waits_resolved": 0,
              "deferred_woken": 0, "schedule_claims_recovered": 0}
    now = epoch_now()

    # ── Expired commands (deadline passed, non-terminal) ────────────────
    expired = await _expire_deadlined(command_repo, batch)
    report["expired"] = expired

    # ── Cancellation acknowledgements aging without a worker ack ────────
    report["cancellation_escalations"] = await _escalate_cancellations(
        command_repo)

    # ── Outbox: publish committed-but-unpublished events (§4.8) ─────────
    try:
        from backend.repositories.factory import get_dispatcher_bus
        bus = get_dispatcher_bus()
        if bus is not None:
            report["outbox_published"] = await run_dispatcher(
                command_repo, bus, owner="reconciler")
    except Exception:
        logger.debug("Outbox dispatch pass failed", exc_info=True)

    # ── Parent waits whose child already reached a terminal state ───────
    if wait_repo is not None and workflow_repo is not None and message_bus is not None:
        try:
            report["parent_waits_resolved"] = await _resolve_parent_waits(
                wait_repo, workflow_repo, message_bus, batch, mcp_repo=mcp_repo)
        except Exception:
            logger.debug("Parent-wait reconciliation failed", exc_info=True)

    # ── §4.11: wake deferred tasks whose persisted wait has elapsed ──────
    if workflow_repo is not None and message_bus is not None:
        try:
            report["deferred_woken"] = await _wake_due_deferrals(
                workflow_repo, message_bus, batch)
        except Exception:
            logger.debug("Deferred-task wake failed", exc_info=True)

    # ── §4.16 (C11): stale schedule claims live on the SCHEDULE repo ────
    if schedule_repo is not None:
        try:
            report["schedule_claims_recovered"] = await _recover_stale_claims(
                schedule_repo)
        except Exception:
            logger.debug("Schedule-claim recovery failed", exc_info=True)

    for key, value in report.items():
        if value:
            METRICS[f"reconciled_{key}"] = METRICS.get(f"reconciled_{key}", 0) + value
    return report


async def _resolve_parent_waits(wait_repo, workflow_repo, message_bus,
                                batch: int, mcp_repo=None) -> int:
    """C8 (§4.13): resolve waits whose child reached a terminal state AND
    actually WAKE the parent — a conditional wait resolution plus a durable
    retrigger of the parked parent task. Resolution is once-only; the wake
    is deduped by the parent task's approval/claim machinery."""
    from datetime import datetime, timezone
    from backend.engine.repo_adapter import maybe_await
    from backend.models.schemas import TaskTriggerEvent
    resolved = 0
    waits = await wait_repo.unresolved_waits(limit=batch) if \
        hasattr(wait_repo, "unresolved_waits") else []
    for wait in waits:
        child_run_id = wait.get("child_run_id")
        if not child_run_id:
            continue
        child = await maybe_await(workflow_repo.get_run(child_run_id))
        if not child or child.get("status") not in ("COMPLETED", "FAILED", "CANCELLED"):
            continue
        outcome = "resolved" if child.get("status") == "COMPLETED" else "failed"
        build_task = next((task for task in child.get("tasks", []) if task.get("agent") == "core.mcp_build"), None)
        if outcome == "resolved" and build_task:
            if mcp_repo is None:
                continue
            from backend.services.connector_eligibility import evaluate_capability_readiness
            import json
            inputs = build_task.get("input_data") or {}
            if isinstance(inputs, str):
                inputs = json.loads(inputs)
            build = await mcp_repo.get_build(inputs.get("build_id"))
            if not build or build.get("user_id") != child.get("user_id"):
                continue
            connector = await mcp_repo.get_mcp(build.get("mcp_id")) if build.get("mcp_id") else None
            runs = await mcp_repo.list_validation_runs(build["mcp_id"]) if connector else []
            if not evaluate_capability_readiness(connector, build, runs)["ready"]:
                continue
        if callable(getattr(wait_repo, "resolve_with_wake", None)):
            payload = {"run_id": wait["parent_run_id"], "task_id": wait["parent_task_id"],
                       "child_run_id": child_run_id, "command_type": "workflow", "outcome": outcome,
                       "wait_id": wait["wait_id"]}
            resolved += int(await wait_repo.resolve_with_wake(wait, payload, outcome=outcome))
            continue
        if not await wait_repo.resolve_wait(wait["wait_id"], outcome):
            continue   # another resolver won; never wake twice
        resolved += 1
        if outcome != "resolved":
            continue   # child failure → explicit parent outcome via the
                       # build record's own failure handling; no blind wake
        parent_run_id = wait.get("parent_run_id")
        parent_task_id = wait.get("parent_task_id")
        if not parent_run_id or not parent_task_id:
            continue
        try:
            await maybe_await(workflow_repo.update_run_status(parent_run_id, "RUNNING"))
            await message_bus.publish("agentos-workflow-events", TaskTriggerEvent(
                workflow_id=(child.get("workflow_id") or parent_run_id),
                run_id=parent_run_id, task_id=parent_task_id,
            ).model_dump(mode="json"))
            logger.info("Parent wait resolved: waking %s/%s after child %s",
                        parent_run_id[:12], parent_task_id[:12], child_run_id[:12])
        except Exception:
            logger.warning("Parent wake publish failed for %s", parent_run_id,
                           exc_info=True)
    return resolved


async def _wake_due_deferrals(workflow_repo, message_bus, batch: int) -> int:
    """§4.11: tasks whose persisted next_attempt_at has elapsed and whose
    status is still PENDING (deferred before claim) get a fresh trigger. The
    attempt claim dedups against any in-process fast-path retrigger."""
    from backend.engine.repo_adapter import maybe_await
    from backend.models.schemas import TaskTriggerEvent
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        due = await maybe_await(workflow_repo.find_due_deferred_tasks(
            now_iso, limit=batch)) if hasattr(workflow_repo, "find_due_deferred_tasks") else None
    except Exception:
        return 0
    if not due:
        return 0
    woken = 0
    for row in due:
        try:
            await message_bus.publish("agentos-workflow-events", TaskTriggerEvent(
                workflow_id=row["workflow_id"], run_id=row["run_id"],
                task_id=row["task_id"]).model_dump(mode="json"))
            woken += 1
        except Exception:
            logger.debug("Deferred wake publish failed", exc_info=True)
    return woken


async def _recover_stale_claims(schedule_repo) -> int:
    """§4.16 (C11): a process killed before releasing its occurrence claim leaves
    the claim without a run. If no run materialized within the recovery
    horizon, the claim is released so redelivery/re-claim can proceed."""
    from datetime import timedelta
    from backend.engine.repo_adapter import maybe_await
    horizon_epoch = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp())
    recovered = 0
    stale = await maybe_await(schedule_repo.find_stale_occurrence_claims(
        horizon_epoch, limit=50))
    for row in stale or []:
        if await maybe_await(schedule_repo.release_occurrence(
                row["schedule_id"], row["occurrence"])):
            recovered += 1
    return recovered


async def _expire_deadlined(command_repo, batch: int) -> int:
    now = epoch_now()
    expired = 0
    try:
        from backend.repositories.admission_repository import (
            get_command_admission_repo,
        )
    except Exception:
        return 0
    # Deadline expiry rides the pending-command sweep's expiry plus explicit
    # command transitions for deadline_at-bearing rows.
    try:
        # §4.18 (C12): backend-neutral query — works on SQLite AND Firestore.
        rows = await command_repo.find_deadline_expired(now, limit=batch)
    except Exception:
        return 0
    for row in rows:
        try:
            await command_repo.transition(
                row["command_id"], CommandState.EXPIRED,
                actor="reconciler", reason="deadline passed",
                terminal_reason="COMMAND_EXPIRED")
            expired += 1
        except Exception:
            logger.debug("Expiry transition skipped for %s",
                         row["command_id"], exc_info=True)
    return expired


async def _escalate_cancellations(command_repo) -> int:
    """cancel_requested older than the ack SLA without acknowledgement is
    escalated visibly — never silently dropped (§4.18)."""
    sla_seconds = 120
    now = epoch_now()
    try:
        rows = await command_repo.find_unacknowledged_cancellations(
            now - sla_seconds, limit=50)
    except Exception:
        return 0
    for row in rows:
        METRICS["cancellation_escalations"] = \
            METRICS.get("cancellation_escalations", 0) + 1
        logger.warning(
            "Cancellation awaiting acknowledgement beyond SLA: command %s "
            "(requested %ss ago) — worker may be lost; lease TTL will free "
            "its capacity", row["command_id"][:16],
            now - int(row["cancel_requested_at"]))
    return len(rows)
