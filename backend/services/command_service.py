"""Command acceptance service (Phase 4 §4.4).

The ONE entry contract every accepted instruction passes: validate → resolve
actor/ownership → resolve the SCOPED idempotency key → compare the request
digest → check command admission → persist command + accounting + initial
work + outbox event ATOMICALLY (§4.5) → return stable identifiers.

Idempotency contract (§4.4):
- new key                 → one command created
- same tenant/type/key + same payload → the EXISTING command returned
- same tenant/type/key + different payload → IDEMPOTENCY_CONFLICT
- same raw key, other tenant → independent scoped command
- replay after terminal   → the original terminal result (within retention)
- intentional rerun       → a NEW idempotency key

Authorization is re-checked on replay by the callers (knowing a key grants
no access to a command). Cancellation intent is recorded through the same
conditional-transition repository (§4.15) — no unrestricted status edits.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from backend.models.command import (
    Command,
    CommandErrorCode,
    CommandReceipt,
    CommandState,
    OutboxEvent,
    new_command_id,
    new_event_id,
    scoped_idempotency_key,
)
from backend.repositories.command_repository import STATUS_URL

logger = logging.getLogger(__name__)


class CommandServiceMetrics:
    def __init__(self) -> None:
        self.counters: Dict[str, int] = {}

    def incr(self, name: str, labels: Optional[Dict[str, str]] = None) -> None:
        key = name
        if labels:
            flat = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if v is not None)
            key = f"{name}{{{flat}}}"
        self.counters[key] = self.counters.get(key, 0) + 1


METRICS = CommandServiceMetrics()


def command_service_metrics() -> Dict[str, Any]:
    return {"counters": dict(METRICS.counters)}


_repo = None


def set_command_repo(repo) -> None:
    global _repo
    _repo = repo


def get_command_repo():
    return _repo



# ── §12.8: backend-neutral initial-work translation ────────────────────
# Typed items describe the SAME logical work for both stores; each store's
# semantics (create-if-absent, one transaction, deterministic ids) are
# preserved by the translation — never by the caller choosing per store.

def translate_initial_work_items(items):
    """Translate typed initial-work items into (sqlite_callback,
    firestore_triples). Supported item kinds:
    - ("parent_wait", {...parent_waits row...})
    - ("build_record", {...mcp_builds row...})
    - ("workflow_run_with_task", {"run": {...}, "task": {...}})
    """
    async def _sqlite_cb(conn):
        for kind, data in items:
            if kind == "parent_wait":
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO parent_waits
                        (wait_id, parent_run_id, parent_task_id, child_command_id,
                         child_run_id, condition, state, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (data["wait_id"], data["parent_run_id"],
                     data["parent_task_id"], data["child_command_id"],
                     data["child_run_id"], data["condition"],
                     data.get("state", "waiting"), data["created_at"]))
            elif kind == "build_record":
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO mcp_builds (
                        build_id, user_id, name, method, source, status, stage,
                        logs_json, origin, parent_run_id, parent_workflow_id,
                        parent_task_id, wait_reason, organization_id,
                        credential_reference, required_operations_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (data["build_id"], data["user_id"], data.get("name", ""),
                     data["method"], data.get("source", ""), "queued", "queued",
                     "[]", data.get("origin", "api"), data.get("parent_run_id"),
                     data.get("parent_workflow_id"), data.get("parent_task_id"),
                     data.get("wait_reason", "capability_missing"),
                     data.get("organization_id"),
                     data.get("credential_reference"),
                     json.dumps(data.get("required_operations") or []),
                     data["created_at"], data["updated_at"]))
            elif kind == "workflow_run_with_task":
                run, task = data["run"], data["task"]
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO workflow_runs
                        (run_id, workflow_id, user_id, goal, status, created_at,
                         updated_at, parent_run_id, thread_id, org_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run["run_id"], run["workflow_id"], run["user_id"],
                     run["goal"], "RUNNING", run["created_at"],
                     run["updated_at"], run.get("parent_run_id"),
                     run.get("thread_id"),
                     run.get("org_id") or run.get("organization_id")))
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO tasks
                        (task_id, run_id, workflow_id, user_id, agent, tool, status,
                         input_data, dependencies, timeout_seconds, max_retries,
                         recovery_enabled)
                    VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
                    """,
                    (task["task_id"], task["run_id"], task["workflow_id"],
                     task["user_id"], task["agent"], task["tool"],
                     task["input_data"], task.get("dependencies", "[]"),
                     task.get("timeout_seconds", 300),
                     task.get("max_retries", 2),
                     task.get("recovery_enabled", 1)))
            else:
                raise TypeError(f"Unknown initial-work item kind: {kind}")

    triples = []
    for kind, data in items:
        if kind == "parent_wait":
            triples.append(("parent_waits", data["wait_id"], data))
        elif kind == "build_record":
            triples.append(("mcp_builds", data["build_id"], {"status": "queued", "stage": "queued", **data}))
        elif kind == "workflow_run_with_task":
            run = dict(data["run"])
            run.setdefault("status", "RUNNING")
            task = {"status": "PENDING", **data["task"]}
            run["tasks"] = [task]
            triples.append(("workflow_runs", run["run_id"], run))
        else:
            raise TypeError(f"Unknown initial-work item kind: {kind}")
    return _sqlite_cb, triples


async def accept_command(
    *,
    command_type: str,
    owner_user_id: str,
    organization_id: Optional[str] = None,
    actor_type: str = "user",
    source: str = "",
    idempotency_key: Optional[str] = None,
    request_digest: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    build_id: Optional[str] = None,
    parent_command_id: Optional[str] = None,
    parent_run_id: Optional[str] = None,
    parent_task_id: Optional[str] = None,
    deadline_at: Optional[int] = None,
    max_pending_per_user: int = 0,
    max_wait_seconds: int = 2 * 3600,
    event_type: str = "command.accepted",
    initial_work: Optional[Callable[[Any], Awaitable[None]]] = None,
    initial_work_items: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
    command_id: Optional[str] = None,
) -> CommandReceipt:
    """Accept one instruction under the §4.4/§4.5 contract. The outbox event
    commits with the command; publication is the dispatcher's recoverable job
    (§4.8). Raises nothing for denial-shaped outcomes — those come back as
    receipts with accepted=False and a structured error code."""
    repo = get_command_repo()
    if repo is None:
        raise RuntimeError("Command repository not configured (§4.5: acceptance "
                           "must be durable; refusing in-memory acceptance)")
    command_id = command_id or new_command_id()
    scoped = scoped_idempotency_key(owner_user_id, command_type, idempotency_key)
    command = Command(
        command_id=command_id,
        command_type=command_type,
        owner_user_id=owner_user_id,
        organization_id=organization_id,
        actor_type=actor_type,
        source=source,
        idempotency_key=idempotency_key,
        idempotency_scoped=scoped,
        request_digest=request_digest,
        payload=payload or {},
        run_id=run_id,
        build_id=build_id,
        parent_command_id=parent_command_id,
        parent_run_id=parent_run_id,
        parent_task_id=parent_task_id,
        deadline_at=deadline_at,
    )
    outbox = OutboxEvent(
        event_id=new_event_id(),
        command_id=command.command_id,
        aggregate_id=command.command_id,
        aggregate_version=1,
        event_type=event_type,
        payload={
            "command_id": command.command_id,
            "command_type": command.command_type,
            "owner_user_id": owner_user_id,
            "organization_id": organization_id,
            "run_id": run_id,
            "build_id": build_id,
            "payload": command.payload,
        },
    )
    admission = SimpleNamespace(max_pending_per_user=max_pending_per_user,
                                max_wait_seconds=max_wait_seconds)
    # §12.8 (review finding I): the BACKEND-NEUTRAL initial-work contract.
    # Callers supply typed `initial_work_items` — the same logical work
    # description for both stores; the service translates:
    #   SQLite  → a connection callback executed inside the owned
    #             transaction (INSERT OR IGNORE semantics preserved);
    #   Firestore → deterministic (collection, doc_id, data) triples
    #             created-if-absent inside the transaction.
    # The legacy raw `initial_work` callback stays for existing SQLite
    # callers; supplying it against a Firestore repo still fails loud.
    import inspect
    repo_accepts_work_writes = False
    try:
        sig = inspect.signature(repo.accept_command_with_initial_work)
        repo_accepts_work_writes = "work_writes" in sig.parameters
    except (TypeError, ValueError):
        pass
    sqlite_cb = initial_work
    work_triples = None
    if initial_work_items:
        sqlite_cb, work_triples = translate_initial_work_items(
            initial_work_items)
        if repo_accepts_work_writes and initial_work is not None:
            raise TypeError(
                "Supply initial_work OR initial_work_items, not both")
    if repo_accepts_work_writes and initial_work is not None:
        # Firestore cannot execute callbacks inside rerunnable
        # transactions — but the POSTGRES V2 repository accepts BOTH
        # shapes (its adapter executes the sqlite-dialect callback via
        # the §21.4 translation layer). Repos declare their capability
        # explicitly; unknown mixed supply still fails loud.
        if not getattr(repo, "accepts_initial_work_callbacks", False):
            raise TypeError(
                "This command repository requires deterministic "
                "work_writes, not an initial_work callback (§10.1 "
                "requirement 6 — backend-neutral contract). Supply "
                "initial_work_items instead — the service translates the "
                "same description for both stores.")
    if repo_accepts_work_writes and initial_work is None:
        # Firestore repo: deterministic (collection, doc_id, data) triples.
        receipt = await repo.accept_command_with_initial_work(
            command, admission, outbox, work_writes=work_triples)
    else:
        # SQLite repo: the translated connection callback.
        receipt = await repo.accept_command_with_initial_work(
            command, admission, outbox, initial_work=sqlite_cb)
    if receipt.accepted and not receipt.deduplicated:
        METRICS.incr("commands_accepted", {"type": command_type})
    elif receipt.deduplicated:
        METRICS.incr("commands_replayed", {"type": command_type})
    else:
        METRICS.incr("commands_rejected",
                     {"type": command_type,
                      "reason": receipt.error_code.value if receipt.error_code else "?"})
    return receipt


async def request_cancellation(command_id: str, by: str, reason: str = "",
                               workflow_repo=None) -> dict:
    """§4.15 (C6): record cancellation intent AND cancel the associated run —
    the engine's run-level cancellation is the mechanism that actually stops
    task work; the command state records the protocol truthfully."""
    repo = get_command_repo()
    if repo is None:
        raise RuntimeError("Command repository not configured")
    command = await repo.record_cancellation_intent(command_id, by, reason)
    METRICS.incr("cancellations_requested")

    # C6 bridge: persist run-level cancellation intent so the engine's
    # existing run-cancellation checks (claim, pre-dispatch, completion)
    # observe it. Terminal commands never rewrite their outcome.
    run_cancelled = False
    if workflow_repo is not None and command.run_id and not command.terminal:
        try:
            cancel = getattr(workflow_repo, "cancel_run", None)
            if cancel is not None:
                run_cancelled = bool(await cancel(command.run_id, by=by,
                                                  reason=reason or "command cancelled"))
        except Exception:
            logger.warning("Run cancellation bridge failed for %s",
                           command.run_id, exc_info=True)

    return {
        "command_id": command.command_id,
        "state": command.state.value,
        "cancel_requested_at": command.cancel_requested_at,
        "terminal": command.terminal,
        "run_cancelled": run_cancelled,
        "message": (
            f"Command already finished ({command.state.value}) before "
            "cancellation took effect." if command.terminal
            else "Cancellation requested. Stopping is acknowledged separately "
                 "by the executing worker."),
    }


async def get_receipt(command_id: str) -> Optional[dict]:
    repo = get_command_repo()
    if repo is None:
        return None
    command = await repo.get_command(command_id)
    if command is None:
        return None
    return {
        "command_id": command.command_id,
        "run_id": command.run_id,
        "build_id": command.build_id,
        "state": command.state.value,
        "state_version": command.state_version,
        "waiting_reason": command.waiting_reason.value if command.waiting_reason else None,
        "terminal": command.terminal,
        "terminal_reason": command.terminal_reason,
        "cancel_requested_at": command.cancel_requested_at,
        "cancel_acknowledged_at": command.cancel_acknowledged_at,
        "result_reference": command.result_reference,
        "created_at": command.created_at,
        "updated_at": command.updated_at,
        "status_url": STATUS_URL.format(command_id=command.command_id),
    }
