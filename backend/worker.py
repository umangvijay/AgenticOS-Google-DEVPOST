import logging
from typing import Optional
import time
import asyncio
import os
from datetime import datetime, timezone
from backend.engine.repo_adapter import persist_task, load_run
from backend.models.schemas import TaskTriggerEvent
from backend.models.schedule import SchedulerTriggerEvent
from backend.repositories.message_bus import MessageBus, MessageContext
from backend.repositories.schedule_repository import ScheduleRepository
from backend.engine.engine import WorkflowEngine
from backend.engine.repo_adapter import maybe_await

logger = logging.getLogger(__name__)

_worker_factory = None   # Phase 18: set at worker boot


async def authorize_background_execution(repo, run_id: str, task_id: str,
                                         *, user_repo=None, org_repo=None,
                                         audit=None) -> Optional[str]:
    """Phase 1.12 / Phase 4 §4.0B: the CURRENT-authority gate for delayed
    work, shared by the worker loop and tests (no mirrored logic). Returns
    the denial reason and audits it, or None when execution may proceed.

    Denial reasons:
    - 'owner_missing'       — the owner account no longer exists. Missing
      identity is an explicit policy (deny), never implicit permission
      (§4.0B: "Missing identity must have an explicit policy").
    - 'owner_disabled'      — the owner's account is disabled.
    - 'org_membership_lost' — an organization-owned run whose owner lost
      membership (or the org vanished).
    When the required repository is unavailable the check CANNOT run: that
    is a configuration failure, not an allow. Production boot passes all
    repositories; local dev may degrade with a loud log (never silently).
    """
    run = await maybe_await(repo.get_run(run_id))
    if not run:
        return None
    deny_reason = None
    if user_repo is None:
        # Phase 4 §4.0B: without the repository the current-authority check
        # cannot run. Fail closed with a loud, actionable message.
        logger.error(
            "Background authorization CANNOT run for run %s: user_repo is not "
            "configured. Denying queued work (fail closed).", str(run_id)[:12])
        deny_reason = "authorization_unavailable"
    else:
        owner = await maybe_await(user_repo.get_by_id(run.get("user_id")))
        if owner is None:
            deny_reason = "owner_missing"
        elif not owner.get("is_active", True):
            deny_reason = "owner_disabled"
    if deny_reason is None and run.get("org_id"):
        if org_repo is None:
            logger.error(
                "Background authorization CANNOT check org %s for run %s: "
                "org_repo is not configured. Denying queued work (fail closed).",
                run["org_id"], str(run_id)[:12])
            deny_reason = "authorization_unavailable"
        else:
            role = await maybe_await(
                org_repo.get_member_role(run["org_id"], run.get("user_id")))
            org = await maybe_await(org_repo.get_org(run["org_id"]))
            if org is None or role is None:
                deny_reason = "org_membership_lost"
    if deny_reason:
        logger.warning(
            "Background authorization denied: owner %s (%s) (run %s task %s)",
            run.get("user_id"), deny_reason, str(run_id)[:8], task_id)
        if audit is not None:
            try:
                from backend.repositories.audit_repository import AuditEvent, ActorType
                await audit.log_event(AuditEvent(
                    event_type="BACKGROUND_AUTHORIZATION_DENIED",
                    actor_id=run.get("user_id"), actor_type=ActorType.SYSTEM,
                    resource_id=run_id,
                    details={"task_id": task_id, "reason": deny_reason},
                ))
            except Exception:
                logger.debug("Denial audit failed", exc_info=True)
    return deny_reason


async def _terminate_denied_task(workflow_engine, run_id: str, task_id: str,
                                 reason: str) -> None:
    """§4.0B: denied queued work gets a TERMINAL lifecycle — acknowledging
    the message while leaving the task indefinitely pending is not a
    complete lifecycle. The task is failed with a structured reason the
    frontend can render; the run evaluates to its honest terminal state."""
    try:
        from backend.models.schemas import TaskStatus
        repo = workflow_engine.repo
        run = await maybe_await(repo.get_run(run_id))
        task = next((t for t in (run.get("tasks") or [])
                     if t.get("task_id") == task_id), None) if run else None
        if task is None:
            return
        from backend.engine.repo_adapter import persist_task, load_run
        run_model = await load_run(repo, run_id)
        if not run_model:
            return
        task_model = next((t for t in run_model.tasks if t.task_id == task_id), None)
        if task_model is None:
            return
        task_model.status = TaskStatus.FAILED
        task_model.error = f"Execution not authorized: {reason}"
        task_model.completed_at = datetime.now(timezone.utc)
        await persist_task(repo, run_id, task_model)
        from backend.models.schemas import WorkflowEventType
        workflow_engine._emit_event(
            WorkflowEventType.TASK_FAILED, run_id, run_model.workflow_id, task_id,
            TaskStatus.FAILED, f"Task {task_id} denied: {reason}")
        await workflow_engine.evaluate_dag(run_id)
    except Exception:
        logger.exception("Could not terminate denied task %s/%s", run_id, task_id)


async def run_feedback_retention(feedback_repo, start_worker=None) -> int:
    """§27.D7: one bounded feedback-retention pass per day per store.
    Returns the removed count (0 when disabled/unavailable)."""
    from backend.config.settings import settings as app_settings
    feedback_days = int(getattr(app_settings, "FEEDBACK_RETENTION_DAYS", 0) or 0)
    if feedback_days <= 0:
        return 0
    marker = "_last_feedback_retention"
    last = getattr(start_worker, marker, None) if start_worker is not None else None
    if last == datetime.now(timezone.utc).date():
        return 0
    if feedback_repo is None or not hasattr(feedback_repo, "apply_retention"):
        logger.debug("Feedback retention: no compatible store wired")
        return 0
    removed = await maybe_await(feedback_repo.apply_retention(feedback_days))
    if start_worker is not None:
        setattr(start_worker, marker, datetime.now(timezone.utc).date())
    logger.info("Feedback retention: %d row(s) older than %dd removed",
                removed, feedback_days)
    return int(removed or 0)


async def start_worker(message_bus: MessageBus, workflow_engine: WorkflowEngine, schedule_repo: ScheduleRepository = None, token_denylist_repo=None, audit_repo=None, idempotency_repo=None, user_repo=None, org_repo=None, mcp_repo=None):
    start_worker._audit_ref = audit_repo
    start_worker._org_repo_ref = org_repo
    start_worker._mcp_repo_ref = mcp_repo
    start_worker._idempotency_repo_ref = idempotency_repo
    logger.info("Worker started, listening for events...")

    async def process_task(run_id: str, task_id: str):
        # Both transport deliveries and database recovery use current authority.
        denial = await authorize_background_execution(
            workflow_engine.repo, run_id, task_id, user_repo=user_repo,
            org_repo=org_repo, audit=audit_repo)
        if denial:
            await _terminate_denied_task(workflow_engine, run_id, task_id, denial)
            return
        await workflow_engine.execute_task(run_id, task_id)
        await _resume_build_waiter(run_id, task_id)
        await _evaluate_terminal_run(run_id)

    async def handle_event(event, ctx: MessageContext):
        try:
            if isinstance(event, dict) and event.get("event_type") == "parent.ready":
                payload = event.get("payload") or {}
                if payload.get("outcome") == "failed":
                    from backend.repositories.wait_repository import get_wait_repo
                    wait_repo = get_wait_repo()
                    wait = await wait_repo.latest_wait(payload["run_id"], payload["task_id"]) if wait_repo else None
                    child = await workflow_engine.repo.get_run(payload["child_run_id"])
                    parent = await workflow_engine.repo.get_run(payload["run_id"])
                    if (wait and wait["wait_id"] == payload.get("wait_id") and wait["state"] == "failed"
                            and wait.get("child_run_id") == payload["child_run_id"]
                            and child and child["status"] in ("FAILED", "CANCELLED")
                            and parent and parent["user_id"] == child["user_id"]):
                        task = next((task for task in parent.get("tasks", [])
                                     if task.get("task_id") == payload["task_id"]), None)
                        if task and task["status"] in ("WAITING", "WAITING_APPROVAL", "PENDING"):
                            await workflow_engine.repo.update_task_if_not_cancelled(
                                payload["run_id"], payload["task_id"],
                                {"status": "BLOCKED", "error": "Required capability build " + child["status"].lower()})
                    else:
                        logger.warning("Ignoring stale or mismatched failed-build wake")
                        await ctx.ack()
                        return
                await workflow_engine.evaluate_dag(payload.get("run_id"))
                await ctx.ack()
                return
            if isinstance(event, dict):
                run_id = event.get("run_id")
                task_id = event.get("task_id")
            else:
                run_id = getattr(event, "run_id", None)
                task_id = getattr(event, "task_id", None)

            # C1 (§5.0 Batch A): command.accepted envelopes from the outbox.
            # A command event needs DAG EVALUATION (eligible tasks), not one
            # arbitrary task trigger — resolve the command's run and evaluate.
            if not task_id and isinstance(event, dict) and \
                    event.get("event_type") == "command.accepted":
                from backend.services.command_service import get_command_repo
                cmd_repo = get_command_repo()
                command = await maybe_await(
                    cmd_repo.get_command(event.get("command_id"))) if cmd_repo else None
                if command is not None and command.run_id:
                    logger.info("Outbox command %s → DAG evaluation for run %s",
                                command.command_id[:16], command.run_id[:12])
                    await workflow_engine.evaluate_dag(command.run_id)
                    await ctx.ack()
                    return
                logger.warning("Command event without a resolvable run: %s",
                               str(event.get("command_id"))[:16])
                await ctx.ack()
                return

            if not run_id or not task_id:
                logger.error("Worker received trigger without run_id/task_id: %s", event)
                await ctx.ack()
                return
            await process_task(run_id, task_id)
            await ctx.ack()
        except Exception as e:
            logger.error(f"Worker encountered unhandled exception: {e}")
            await ctx.nack()

    def ep_summary(ep: dict) -> str:
        """One honest line for the episode memory (§16.16)."""
        return (f"outcome={ep['verified_outcome']} "
                f"family={ep['task_family']} "
                f"failures={len(ep['failures'])}")

    async def _evaluate_terminal_run(run_id: str):
        """Phase 12 §12.11: terminal events enqueue DURABLE evaluation
        jobs — worker crashes and repeated terminal events neither lose
        evaluations nor inflate sample counts (create-or-get job
        identity). The evaluation itself is evidence-contract based and
        create-or-get idempotent."""
        try:
            from backend.repositories.evaluation_job_repository import (
                get_evaluation_job_repo)
            job_repo = get_evaluation_job_repo()
            repo = workflow_engine.repo
            run = await maybe_await(repo.get_run(run_id))
            if not run or run.get("status") not in ("COMPLETED", "FAILED",
                                                    "NEEDS_REVIEW"):
                return
            if job_repo is not None:
                job = await job_repo.enqueue(run_id)
                if job.get("state") == "done":
                    return   # already evaluated (duplicate terminal event)
                # §13.2: claim THIS run's job by id — never the globally
                # oldest one (an unrelated terminal event cannot strand it).
                _worker_id = "evaluation-worker"
                owned = await job_repo.claim_one(job["job_id"],
                                                 _worker_id)
                if owned is None:
                    return   # another worker owns it live
            # Re-load the full model object so task fields are typed.
            from backend.models.schemas import WorkflowRun
            run_model = WorkflowRun.model_validate(run)
            from backend.services.evaluation_service import evaluate_run_async
            record = await evaluate_run_async(
                run_model, getattr(workflow_engine, "db_client", None))
            if job_repo is not None:
                await job_repo.complete(job["job_id"],
                                        evaluation_id=record.evaluation_id,
                                        owner_worker_id=_worker_id)
            # Phase 14 §14.1: join the VERIFIED outcome to this run's
            # routing decisions (exactly once per decision) and update the
            # learned policy only from verified/rejected classes.
            try:
                from backend.services.routing_recorder import (
                    attach_evaluation_feedback)
                from backend.services.adaptive_policy import AdaptivePolicy
                # The FULL durable record carries the outcome fields the
                # reward classification needs (needs-review, effect states).
                evidence: dict = {"success": record.success}
                try:
                    from backend.repositories.evaluation_repository import (
                        get_evaluation_repo)
                    _erepo = get_evaluation_repo()
                    if _erepo is not None:
                        stored = await _erepo.get_evaluation(record.evaluation_id)
                        if stored and stored.get("record"):
                            evidence = stored["record"]
                except Exception:
                    logger.debug("Stored evaluation read failed for %s",
                                 run_id, exc_info=True)
                _learn = await attach_evaluation_feedback(
                    run_id, evidence, evaluation_id=record.evaluation_id,
                    adaptive=AdaptivePolicy(
                        getattr(workflow_engine, "adaptive_policy_repo", None)))
                # Phase 14 §14.18: the CONTEXTUAL policy consumes the same
                # durable reward events (context_key carried on the event)
                # — atomic per-event consumption, never a parallel
                # re-derivation from the decision rows.
                # Phase 16 findings E+F: a VERIFIED outcome becomes
                # execution memory (§16.16) and proposes a procedure
                # candidate (§16.17) — through the governed write policy,
                # release-scoped (§16.32). Uncertain/rejected outcomes
                # record honestly as episodes too; they never propose
                # procedures.
                try:
                    from backend.config.settings import settings as _s
                    from backend.services.memory_governance import (
                        memory_stage_flags,
                    )
                    _flags = memory_stage_flags(_s)
                    if _flags["stages"].get("verified_execution_history"):
                        from backend.services.memory_lifecycle import (
                            MemoryLifecycleService,
                        )
                        from backend.services.memory_workflows import (
                            episode_record,
                        )
                        from backend.repositories.sqlite.memory_repository import (
                            SQLiteMemoryRepository,
                        )
                        _mem_repo = getattr(workflow_engine,
                                            "memory_repo", None)
                        if _mem_repo is not None:
                            _lc = MemoryLifecycleService(_mem_repo)
                            _ep = episode_record(
                                run_id=run_id,
                                goal=str(run.get("goal") or "")[:200],
                                task_family=str(
                                    (run.get("workflow_id") or "workflow")),
                                environment={"outcome_class":
                                             _learn.get("reward_class")},
                                tools_used=[], decisions=[],
                                failures=[], cost_units=0.0,
                                duration_ms=None,
                                verified_outcome=(
                                    "verified" if _learn.get("reward_class")
                                    == "verified" else "uncertain"),
                                limitations=None)
                            await _lc.remember(
                                user_id=str(run.get("user_id") or ""),
                                content=f"run {run_id}: {ep_summary(_ep)}",
                                memory_class="execution_episode",
                                source="evaluation:evidence-joined",
                                subject_key=f"episode:{run_id}",
                                embedding=[0.0],
                                metadata={"episode": _ep,
                                          "scope": "personal",
                                          "verification_status": (
                                              "confirmed" if
                                              _ep["verified_outcome"]
                                              == "verified" else "unverified"),
                                          "source_event_id":
                                              f"episode:{run_id}:v1"})
                            # §16.17 (finding F): a VERIFIED episode
                            # proposes a procedure CANDIDATE — stored
                            # as needing confirmation (never
                            # auto-activated); review promotes it.
                            if _ep["verified_outcome"] == "verified" and \
                                    _flags["stages"].get(
                                        "reviewed_procedures"):
                                await _lc.remember(
                                    user_id=str(run.get("user_id") or ""),
                                    content=(
                                        f"procedure candidate: repeat the "
                                        f"monthly-report flow that "
                                        f"succeeded in run {run_id} "
                                        "(preconditions: current "
                                        "connector readiness, bound "
                                        "credential, budget)"),
                                    memory_class="procedure",
                                    source="episode:verified",
                                    subject_key=f"procedure:{run_id}",
                                    embedding=[0.0],
                                    metadata={
                                        "scope": "personal",
                                        "verification_status":
                                            "unverified",
                                        "policy_decision":
                                            "needs_confirmation",
                                        "source_event_id":
                                            f"procedure:{run_id}:v1"})
                except Exception:
                    logger.debug("Execution-episode recording failed",
                                 exc_info=True)
                _event_ids = _learn.get("reward_events") or []
                if _event_ids:
                    try:
                        from backend.services.contextual_policy import (
                            ContextualRoutingPolicy,
                        )
                        _ctx_repo = getattr(workflow_engine,
                                            "adaptive_policy_repo", None)
                        if _ctx_repo is not None and hasattr(
                                _ctx_repo, "consume_reward_event"):
                            _cp = ContextualRoutingPolicy(_ctx_repo)
                            for _ev in _event_ids:
                                await _cp.consume_reward_event(_ev)
                    except Exception:
                        logger.debug("Contextual learning failed for %s",
                                     run_id, exc_info=True)
            except Exception:
                logger.debug("Routing feedback join failed for %s", run_id,
                             exc_info=True)
        except Exception as exc:
            # §12.11: the durable job requeues (bounded) — failures are
            # visible in the job health, never silently dropped.
            try:
                from backend.repositories.evaluation_job_repository import (
                    get_evaluation_job_repo)
                job_repo = get_evaluation_job_repo()
                if job_repo is not None:
                    await job_repo.fail_active_for_run(
                        run_id, str(exc), owner_worker_id="evaluation-worker")
            except Exception:
                logger.debug("Evaluation job failure recording failed",
                             exc_info=True)
            logger.debug("Run evaluation failed for %s", run_id, exc_info=True)

    async def _resume_build_waiter(run_id: str, task_id: str):
        """v5.6 A10: when a queued capability build completes, auto-resume the
        orchestrator task that parked on it. The build record carries the
        parent linkage; the parked task is a durable approval bound to the
        build — resolved here and re-dispatched with the approval bound to
        the task (mirrors the approvals endpoint's resume logic)."""
        try:
            repo = workflow_engine.repo
            run = await maybe_await(repo.get_run(run_id))
            if not run:
                return
            completed = next((t for t in (run.get("tasks") or []) if t.get("task_id") == task_id), None)
            if not completed or completed.get("agent") != "core.mcp_build":
                return
            from backend.repositories.wait_repository import get_wait_repo
            from backend.services.command_reconciler import _resolve_parent_waits
            wait_repo = get_wait_repo()
            if wait_repo is not None:
                await _resolve_parent_waits(wait_repo, repo, message_bus, batch=100,
                                           mcp_repo=getattr(start_worker, "_mcp_repo_ref", None))
            import json
            build_inputs = completed.get("input_data") or {}
            if isinstance(build_inputs, str):
                build_inputs = json.loads(build_inputs)
            build_id = build_inputs.get("build_id")
            mcp_repo = getattr(start_worker, "_mcp_repo_ref", None)
            if not build_id or mcp_repo is None:
                logger.error("Build-waiter cannot resolve build %s: mcp_repo not configured at worker boot", str(build_id)[:8])
                return
            build = await maybe_await(mcp_repo.get_build(build_id))
            parent_run_id = build.get("parent_run_id")
            parent_task_id = build.get("parent_task_id")
            if not parent_run_id or not parent_task_id or build.get("status") != "success":
                return
            # §12.7 (review finding H): the parent resumes when the
            # CAPABILITY is READY under current policy — registered,
            # credential-bound, required operations tested, revision
            # ACTIVATED — never merely because generation finished. A
            # registered-but-disabled or untested connector parks the
            # parent with the precise next action; the candidate is
            # preserved and never rebuilt redundantly.
            from backend.services.connector_eligibility import (
                evaluate_capability_readiness,
            )
            mcp_record = None
            validation_runs = []
            if build.get("mcp_id"):
                try:
                    mcp_record = await maybe_await(
                        mcp_repo.get_mcp(build["mcp_id"]))
                except Exception:
                    mcp_record = None
                # §13.3: readiness reads PASSING validation evidence from
                # the durable runs — the caller fetches, the gate judges.
                try:
                    validation_runs = await maybe_await(
                        mcp_repo.list_validation_runs(build["mcp_id"]))
                except Exception:
                    validation_runs = []
            readiness = evaluate_capability_readiness(mcp_record, build,
                                                      validation_runs)
            if not readiness["ready"]:
                try:
                    from backend.services.notification_service import (
                        NotificationService)
                    await NotificationService(
                        getattr(workflow_engine, "notification_repo", None)
                    ).create(
                        user_id=build.get("user_id") or "",
                        notification_type="SYSTEM",
                        title="Capability built — one step before your task can resume",
                        body=readiness["next_action"] or "Complete the connector setup to resume the original task.",
                        metadata={"build_id": build_id,
                                  "readiness_state": readiness["state"],
                                  "parent_run_id": parent_run_id,
                                  "parent_task_id": parent_task_id},
                    )
                except Exception:
                    logger.debug("Capability readiness notification failed",
                                 exc_info=True)
                logger.info(
                    "Build %s is not capability-ready (%s): %s — parent %s/%s stays parked",
                    str(build_id)[:8], readiness["state"],
                    readiness["next_action"], parent_run_id, parent_task_id)
                return
            parent_run = await maybe_await(repo.get_run(parent_run_id))
            if not parent_run:
                return
            parent_task = next((t for t in (parent_run.get("tasks") or [])
                                if t.get("task_id") == parent_task_id), None)
            if not parent_task or parent_task.get("status") != "WAITING_APPROVAL":
                return
            pending = await maybe_await(repo.list_pending_approvals(parent_run.get("user_id") or ""))
            match = next((a for a in pending
                          if a.get("run_id") == parent_run_id and a.get("task_id") == parent_task_id
                          and ((a.get("arguments") or {}).get("build_id") == build_id)), None)
            if not match:
                return
            resolved = await maybe_await(repo.resolve_approval(
                match["approval_id"], "APPROVED", decision_by="SYSTEM:capability-build"))
            if not resolved:
                return
            idempotency_repo = getattr(start_worker, "_idempotency_repo_ref", None)
            if idempotency_repo is not None and hasattr(idempotency_repo, "reset_failed_task"):
                await maybe_await(idempotency_repo.reset_failed_task(parent_run.get("workflow_id"), parent_task_id))
            fresh_model = await load_run(repo, parent_run_id)
            fresh_task_model = next((t for t in (fresh_model.tasks if fresh_model else [])
                                     if t.task_id == parent_task_id), None) if fresh_model else None
            if fresh_task_model is not None:
                from backend.models.schemas import TaskStatus as _TS
                fresh_task_model.status = _TS.PENDING
                fresh_task_model.error = None
                fresh_task_model.input_data["_approved_request_id"] = match["approval_id"]
                await persist_task(repo, parent_run_id, fresh_task_model)
            await maybe_await(repo.update_run_status(parent_run_id, "RUNNING"))
            await message_bus.publish("agentos-workflow-events", TaskTriggerEvent(
                workflow_id=(fresh_model.workflow_id if fresh_model
                             else parent_run.get("workflow_id")),
                run_id=parent_run_id, task_id=parent_task_id,
            ).model_dump(mode="json"))
            logger.warning("Capability build %s landed; resumed parked task %s/%s",
                           build_id[:8], parent_run_id[:8], parent_task_id)
        except Exception:
            logger.exception("Build-waiter resume failed for %s/%s", run_id, task_id)

    # We will gather all consumers at the end
    
    async def handle_schedule(event, ctx: MessageContext):
        occurrence = None
        claimed = False
        try:
            if not schedule_repo:
                await ctx.ack()
                return

            logger.info(f"Worker received schedule trigger: {event}")
            if isinstance(event, SchedulerTriggerEvent):
                trigger = event
            else:
                trigger = SchedulerTriggerEvent(**event)
            schedule = await maybe_await(schedule_repo.get_schedule(trigger.schedule_id))

            if not schedule or str(schedule.get("status", "")).upper() != "ACTIVE":
                logger.warning(f"Schedule {trigger.schedule_id} not found or not ACTIVE")
                await ctx.ack()
                return

            # Phase 3 §3.15: ATOMIC occurrence claim replaces read-then-start.
            # The stable key is schedule_id + scheduled occurrence; the claim
            # INSERT resolves duplicate deliveries (two workers / redelivery)
            # to exactly one logical execution. The history check below stays
            # as the overlap policy notification layer.
            occurrence = trigger.scheduled_time.isoformat() if hasattr(
                trigger.scheduled_time, "isoformat") else str(trigger.scheduled_time)
            from backend.scheduler.run_factory import scheduled_run_id
            claim_run_id = scheduled_run_id(trigger.schedule_id, trigger.scheduled_time)
            claimed = False
            if hasattr(schedule_repo, "claim_occurrence"):
                claimed = await maybe_await(schedule_repo.claim_occurrence(
                    trigger.schedule_id, occurrence, claim_run_id))
                if not claimed:
                    logger.info(
                        "Schedule %s occurrence %s already claimed by another worker; "
                        "skipping duplicate trigger",
                        trigger.schedule_id, occurrence)
                    await ctx.ack()
                    return
            else:
                claimed = True

            # §4.16 overlap policy (generalizes audit P2.12): deduping one
            # occurrence does not stop two DIFFERENT occurrences from running
            # concurrently. Count the schedule's ACTIVE runs; beyond the cap,
            # skip with the user notification.
            max_concurrent = 1   # default overlap policy: skip
            try:
                active_run_id = None
                active_count = 0
                history = await maybe_await(schedule_repo.get_execution_history(
                    trigger.schedule_id, limit=10)) or []
                for entry in history:
                    prev_run_id = entry.get("run_id")
                    if not prev_run_id:
                        continue
                    prev = await maybe_await(workflow_engine.repo.get_run(prev_run_id))
                    if prev and prev.get("status") in ("PENDING", "RUNNING", "WAITING", "RECOVERING", "RETRYING"):
                        active_count += 1
                        if active_run_id is None:
                            active_run_id = prev_run_id
                if active_count >= max_concurrent:
                    logger.info("Skipping schedule %s trigger: %d active run(s) "
                                "at the overlap cap (latest %s)",
                                trigger.schedule_id, active_count,
                                (active_run_id or "")[:12])
                    await maybe_await(schedule_repo.record_execution(
                        trigger.schedule_id, active_run_id or "overlap-skipped",
                        "SKIPPED_BUSY"))
                    if claimed and hasattr(schedule_repo, "finish_occurrence"):
                        await maybe_await(schedule_repo.finish_occurrence(
                            trigger.schedule_id, occurrence, "skipped_busy"))
                    from backend.services.notification_service import NotificationService
                    await NotificationService(getattr(workflow_engine, "notification_repo", None)).create(
                        user_id=schedule["user_id"],
                        notification_type="SCHEDULE_SKIPPED",
                        title="Schedule skipped",
                        body=f"'{schedule.get('name')}' did not fire: a previous run is still in progress.",
                        metadata={"schedule_id": trigger.schedule_id,
                                  "run_id": active_run_id},
                    )
                    await ctx.ack()
                    return
            except Exception:
                logger.warning("Overlap cap check failed; proceeding with trigger",
                               exc_info=True)

            # Deterministic id: schedule_id + logical scheduled time. The API's
            # run-now creates the same run; create_if_absent keeps it exactly-once.
            # C5 (§5.0 Batch A): scheduled runs enter the SAME command contract —
            # the durable command commits (idempotent by the occurrence key)
            # before the run materializes; C4's bridge keeps its state honest.
            from backend.scheduler.run_factory import build_scheduled_run
            run_id = claim_run_id
            try:
                from backend.services.command_service import accept_command
                await accept_command(
                    command_type="schedule",
                    owner_user_id=schedule["user_id"],
                    actor_type="scheduler", source="scheduler",
                    idempotency_key=f"occurrence:{trigger.schedule_id}:{occurrence}",
                    request_digest=run_id,
                    payload={"schedule_id": trigger.schedule_id,
                             "scheduled_time": occurrence,
                             "goal": schedule.get("goal", "")[:4000]},
                    run_id=run_id,
                    max_pending_per_user=0,
                )
            except Exception:
                logger.debug("Schedule command acceptance skipped", exc_info=True)
            run = build_scheduled_run(schedule, run_id)

            created = await maybe_await(workflow_engine.repo.create_if_absent(run.model_dump(mode="json")))
            if created:
                logger.info(f"Successfully enqueued new run {run_id} for schedule {trigger.schedule_id}")
                try:
                    await maybe_await(schedule_repo.record_execution(trigger.schedule_id, run_id, "STARTED"))
                except Exception:
                    logger.warning("Failed to record schedule execution", exc_info=True)
                
                await workflow_engine.evaluate_dag(run_id)
            else:
                logger.info(f"Run {run_id} already exists; rechecking pending tasks.")
                await workflow_engine.evaluate_dag(run_id)
                
            await ctx.ack()
        except Exception as e:
            logger.error(f"Worker encountered error processing schedule trigger: {e}")
            # §3.15: release the occurrence claim so the redelivered trigger
            # can re-claim — a crash between claim and run-creation must not
            # strand the occurrence as permanently skipped.
            if claimed and occurrence and hasattr(schedule_repo, "release_occurrence"):
                try:
                    await maybe_await(schedule_repo.release_occurrence(
                        trigger.schedule_id, occurrence))
                except Exception:
                    logger.warning("Failed to release schedule occurrence claim",
                                   exc_info=True)
            await ctx.nack()

    async def cron_ticker():
        """Local scheduler: fire due cron schedules (Cloud Scheduler replaces this in prod).

        Catch-up policy: skip missed windows — each due schedule fires at most
        once per pass and next_run_at advances from *now*, so a backlog of
        missed windows during downtime collapses into a single run. A large
        miss is surfaced to the user instead of firing silently."""
        from datetime import datetime, timezone
        from backend.services.notification_service import NotificationService
        notification_repo = getattr(workflow_engine, "notification_repo", None)
        while True:
            try:
                if schedule_repo and hasattr(schedule_repo, "list_due_schedules"):
                    now = datetime.now(timezone.utc)
                    due = await maybe_await(schedule_repo.list_due_schedules(now.isoformat()))
                    for schedule in due:
                        trigger = SchedulerTriggerEvent(
                            schedule_id=schedule["schedule_id"], scheduled_time=datetime.fromisoformat(schedule["next_run_at"].replace("Z", "+00:00"))
                        )
                        # Missed-run alert: the window is far in the past, i.e.
                        # the worker was unavailable when it was due.
                        try:
                            missed_by = (now - trigger.scheduled_time).total_seconds()
                            if missed_by > 300:
                                await NotificationService(notification_repo).create(
                                    user_id=schedule["user_id"],
                                    notification_type="SYSTEM",
                                    title="Schedule caught up",
                                    body=f"'{schedule.get('name')}' missed its {trigger.scheduled_time:%H:%M} UTC window "
                                         f"({int(missed_by // 60)} min late). Running it once now; later missed windows are skipped.",
                                    metadata={"schedule_id": schedule["schedule_id"], "missed_by_seconds": int(missed_by)},
                                )
                        except Exception:
                            logger.debug("Missed-run notification failed", exc_info=True)
                        await message_bus.publish(
                            "agentos-scheduler-triggers", trigger.model_dump(mode="json")
                        )
                        # Advance next_run_at so the schedule doesn't refire.
                        # §29.19 revision: the next occurrence is resolved
                        # under the schedule's PERSISTED missed-run/DST
                        # policy and stored as the (UTC instant, logical
                        # local occurrence) pair.
                        try:
                            from backend.services.schedule_time import (
                                resolve_occurrence)
                            resolved = resolve_occurrence(
                                schedule.get("cron_expression") or "",
                                schedule.get("timezone") or "UTC",
                                after=now,
                                gap_policy=(
                                    "skip" if (schedule.get(
                                        "missed_run_policy") or
                                        "coalesce_one") == "coalesce_one"
                                    else schedule.get("missed_run_policy")),
                                fold_policy=schedule.get("missed_run_policy")
                                if schedule.get("missed_run_policy") in
                                ("first", "second") else "first")
                            next_run = resolved["utc_instant"] if resolved                                 else None
                            next_local = resolved["local_occurrence"]                                 if resolved else None
                        except Exception:
                            next_run = next_local = None
                        await maybe_await(schedule_repo.update_schedule(
                            schedule["schedule_id"],
                            {"next_run_at": next_run,
                             "next_run_local": next_local},
                        ))
            except Exception as e:
                logger.error(f"Cron ticker error: {e}")
            # Phase 18 (C8/C12): the deliverable publication recovery
            # and memory cleanup workers run on the production tick —
            # durable jobs claimed with leases, progress recorded,
            # restart-safe.
            try:
                from backend.repositories.deliverable_repository import (
                    get_deliverable_repo,
                )
                _dlv_repo = get_deliverable_repo()
                if _dlv_repo is not None:
                    from backend.workers.deliverable_worker import (
                        run_publication_recovery,
                    )
                    _done = await maybe_await(
                        run_publication_recovery(_dlv_repo, limit=5))
                    if _done:
                        logger.info("Publication recovery: %d completed",
                                    _done)
            except Exception:
                logger.debug("Publication recovery tick failed",
                             exc_info=True)
            try:
                _mem_repo = getattr(_worker_factory, "memory_repo", None) \
                    if _worker_factory else None
                if _mem_repo is not None:
                    from backend.workers.memory_cleanup_worker import (
                        run_cleanup_pass,
                    )
                    _cleaned = await maybe_await(
                        run_cleanup_pass(_mem_repo, max_jobs=5))
                    if _cleaned:
                        logger.info("Memory cleanup: %d jobs completed",
                                    _cleaned)
            except Exception:
                logger.debug("Memory cleanup tick failed", exc_info=True)
            # §15.8/§15.17 (findings I/J): the delegation reconciler —
            # undrained PROPOSED contracts (the crash-before-publication
            # window) are re-dispatchable, deadline-expired contracts
            # transition honestly to TIMED_OUT, and lost workers' leases
            # surface in the reclaimable queue for claim takeover.
            try:
                from backend.repositories.agent_task_repository import (
                    get_agent_task_repo,
                )
                _contracts = get_agent_task_repo()
                if _contracts is not None and hasattr(
                        _contracts, "expire_stale_contracts"):
                    _expired = await maybe_await(
                        _contracts.expire_stale_contracts())
                    if _expired:
                        logger.info("Contract sweeper: %d deadline-expired "
                                    "delegation(s) → TIMED_OUT", _expired)
            except Exception:
                logger.debug("Contract sweep failed", exc_info=True)
            # Housekeeping: expired denylist entries are dead weight; purge
            # opportunistically instead of needing a separate cron job.
            try:
                if token_denylist_repo is not None:
                    await maybe_await(token_denylist_repo.purge_expired())
            except Exception:
                logger.debug("Denylist purge failed", exc_info=True)
            # Shared rate-limit windows (Phase 3 §3.4): bounded store. The
            # repository deletes only rows whose cleanup_after (window end +
            # one retention window) is behind the cutoff, so a live counter
            # is never swept regardless of the cutoff passed here.
            try:
                from backend.security.shared_rate_limiter import get_shared_rate_repo
                repo = get_shared_rate_repo()
                if repo is not None and hasattr(repo, "purge_expired"):
                    swept = await maybe_await(repo.purge_expired(older_than_epoch=int(time.time())))
                    if swept:
                        logger.debug("Shared rate window purge: %d row(s)", swept)
            except Exception:
                logger.debug("Shared rate window purge failed", exc_info=True)
            # Paused browser sessions (human challenges) hold a whole
            # Chromium; expire leases so worker memory stays bounded (v5.6 A7).
            try:
                from backend.services.browser_sessions import sweep_expired_sessions
                await sweep_expired_sessions()
            except Exception:
                logger.debug("Browser session sweep failed", exc_info=True)
            # The local registry acknowledges durable cleanup only after closing
            # that exact runtime and checking its worker/epoch. An expired DB
            # row alone is not evidence that a remote browser was terminated.
            # Managed executions can expire long after worker startup.
            try:
                from backend.services.runner_bridge import reconcile_runner_once
                await reconcile_runner_once()
            except Exception:
                logger.warning("Runner reconciliation failed", exc_info=True)
            # Capacity leases (Phase 3 §3.11 #8): reclaim units from workers
            # that died mid-flight so tenant caps recover without operator action.
            try:
                from backend.services.capacity_service import reconcile_expired
                await reconcile_expired()
            except Exception:
                logger.debug("Capacity lease reconcile failed", exc_info=True)
            # Command admission (Phase 3 §3.12): free queue slots held past
            # their maximum-wait policy so tenant pending caps stay honest.
            try:
                from backend.services.admission_service import expire_stale
                expired = await expire_stale()
                if expired:
                    logger.info("Command admission: %d stale pending command(s) expired", expired)
            except Exception:
                logger.debug("Pending-command expiry sweep failed", exc_info=True)
            # Crash-orphaned idempotency locks (audit F07): a worker that died
            # between claim and commit leaves 'running' forever. Quarantine it
            # as 'uncertain' — the external outcome is unknown, so blind
            # replay is never offered; an operator or provider receipt
            # reconciles it explicitly.
            try:
                if idempotency_repo is not None and hasattr(idempotency_repo, "sweep_stale_running"):
                    swept = await maybe_await(idempotency_repo.sweep_stale_running(older_than_seconds=1800))
                    if swept:
                        logger.warning("Idempotency sweep: %d stale running execution(s) quarantined as uncertain", swept)
            except Exception:
                logger.debug("Idempotency stale-running sweep failed", exc_info=True)
            # Retention control (enterprise P2): purge old events/audit rows
            # once per day. Workflow runs are never auto-deleted.
            try:
                from backend.config.settings import settings as app_settings
                retention_days = int(getattr(app_settings, "RETENTION_DAYS", 0) or 0)
                if retention_days > 0 and getattr(start_worker, "_last_retention_purge", None) != datetime.now(timezone.utc).date():
                    start_worker._last_retention_purge = datetime.now(timezone.utc).date()
                    repo = workflow_engine.repo
                    if hasattr(repo, "purge_old_events"):
                        removed = await maybe_await(repo.purge_old_events(retention_days))
                        logger.info("Retention purge: %d events older than %dd removed", removed, retention_days)
                    if audit_repo is not None and hasattr(audit_repo, "purge_older_than"):
                        removed = await maybe_await(audit_repo.purge_older_than(retention_days))
                        logger.info("Retention purge: %d audit rows older than %dd removed", removed, retention_days)
            except Exception:
                logger.debug("Retention purge failed", exc_info=True)

            # §27.D7: FEEDBACK retention — bounded daily sweep (audit C-16).
            try:
                feedback_repo = getattr(workflow_engine, "factory", None)
                feedback_repo = getattr(feedback_repo, "feedback_repo", None) \
                    if feedback_repo is not None else None
                if feedback_repo is None:
                    feedback_repo = getattr(_worker_factory, "feedback_repo", None)
                await run_feedback_retention(feedback_repo, start_worker)
            except Exception:
                logger.warning("Feedback retention sweep failed", exc_info=True)

            # ── Budget reconciliation sweep (Phase 2, §2.13/§2.20-11) ──
            # Reservation expiry triggers INVESTIGATION, not release: stale
            # reserved/dispatching holds are marked uncertain (still counted)
            # so repeated crashes cannot silently strand or free allowance.
            try:
                from backend.services.budget_service import get_budget_service
                report = await get_budget_service().sweep_for_reconciliation(
                    older_than_minutes=60, limit=50,
                )
                if report.get("investigated"):
                    logger.info(
                        "Budget reconciliation investigated %d stale reservations (%d marked uncertain)",
                        report["investigated"], report["marked_uncertain"],
                    )
            except Exception:
                logger.debug("Budget reconciliation sweep failed", exc_info=True)

            # ── Command reconciliation + outbox dispatch (Phase 4 §4.18) ──
            # The bounded self-healing loop: expired deadlines, cancellation
            # ack escalation, outbox publication, parent-wait resolution.
            try:
                from backend.services.command_service import get_command_repo
                from backend.services.command_reconciler import reconcile_once
                from backend.repositories.wait_repository import get_wait_repo
                command_repo = get_command_repo()
                if command_repo is not None:
                    report = await reconcile_once(
                        command_repo,
                        wait_repo=get_wait_repo(),
                        workflow_repo=workflow_engine.repo,
                        schedule_repo=schedule_repo,
                        message_bus=message_bus,
                        mcp_repo=mcp_repo,
                    )
                    if any(report.values()):
                        logger.info("Command reconciliation: %s", report)
            except Exception:
                logger.debug("Command reconciliation pass failed", exc_info=True)

            # Phase 13 §13.2: the independent bounded evaluation drainer —
            # processes whichever jobs it claims, for whichever runs they
            # belong to; a crashed claimer's lease expires and another
            # worker finishes the job.
            try:
                from backend.repositories.evaluation_job_repository import (
                    get_evaluation_job_repo)
                _job_repo = get_evaluation_job_repo()
                if _job_repo is not None:
                    from backend.services.evaluation_service import (
                        evaluate_run_async as _eval_run)
                    from backend.models.schemas import WorkflowRun as _WR

                    async def _run_evaluation(job):
                        _run = await maybe_await(workflow_engine.repo.get_run(
                            job["run_id"]))
                        if not _run:
                            raise RuntimeError(f"run {job['run_id']} vanished")
                        record = await _eval_run(
                            _WR.model_validate(_run),
                            getattr(workflow_engine, "db_client", None))
                        return record.evaluation_id

                    drained = await _job_repo.drain("evaluation-drainer",
                                                    batch=5,
                                                    run_evaluation=_run_evaluation)
                    if drained:
                        logger.info("Evaluation drainer completed %d jobs",
                                    drained)
            except Exception:
                logger.debug("Evaluation drain pass failed", exc_info=True)

            # Phase 13 §13.13: the comparison worker — executes pinned
            # experiment cases (bounded batch, resumable per-case records).
            # The case EXECUTOR is deployment-wired; without one nothing
            # runs (the jobs queue, never fabricate).
            try:
                _cmp_repo = getattr(start_worker, "_comparison_repo_ref", None)
                _case_executor = getattr(start_worker,
                                         "_comparison_case_executor", None)
                if _cmp_repo is not None and _case_executor is not None:
                    from backend.services.experiment_comparison import (
                        drain_comparisons,
                    )
                    drained = await drain_comparisons(
                        "comparison-worker", batch=1,
                        run_case=_case_executor)
                    if drained:
                        logger.info("Comparison worker completed %d",
                                    drained)
            except Exception:
                logger.debug("Comparison drain pass failed", exc_info=True)

            # Phase 11 §11.8: recovery episode reconciliation — abandoned
            # episodes (lease expired while active) get marked SUPERSEDED.
            try:
                from backend.repositories.recovery_episode_repository import (
                    get_recovery_episode_repo, RecoveryState,
                )
                ep_repo = get_recovery_episode_repo()
                if ep_repo is not None:
                    for ep in await ep_repo.abandoned_episodes(limit=25):
                        await ep_repo.transition(
                            ep["episode_id"], RecoveryState.SUPERSEDED,
                            actor="reconciler", error_code="LEASE_LOST")
                        logger.info("Recovery episode %s superseded (lease lost)",
                                    ep["episode_id"][:16])
            except Exception:
                logger.debug("Recovery episode reconciliation failed", exc_info=True)
            await asyncio.sleep(30)

    from backend.workers.recovery_worker import RecoveryWorker
    recovery_worker = RecoveryWorker(message_bus, workflow_engine.repo, workflow_engine)

    tasks = [
        message_bus.consume("agentos-workflow-events", handle_event),
        message_bus.consume("agentos-scheduler-triggers", handle_schedule),
        message_bus.consume("agentos-recovery-events", recovery_worker.handle_recovery_event),
        cron_ticker(),
    ]
    # §21.4: the transactional-outbox dispatcher — durable cross-process
    # delivery claimed from the shared Postgres ledger. Runs ALONGSIDE the
    # bus consumers; the command idempotency contract makes double
    # delivery (bus + outbox) safe.
    try:
        from backend.workers.outbox_dispatcher import OutboxDispatcher
        from backend.services.command_service import get_command_repo
        dispatcher = OutboxDispatcher(get_command_repo(), handle_event,
                                      owner=f"worker-{os.getpid()}")
        tasks.append(dispatcher.run())
        logger.info("Outbox dispatcher attached (durable cross-process delivery)")
        from backend.workers.outbox_dispatcher import DirectTaskRunner
        from backend.repositories.postgres.workflow_repository import PostgresWorkflowEngineRepository
        if isinstance(workflow_engine.repo, PostgresWorkflowEngineRepository):
            runner = DirectTaskRunner(workflow_engine.repo, process_task, owner=f"worker-{os.getpid()}")
            tasks.append(runner.run())
            logger.info("Direct task runner attached (DB-driven execution)")
    except Exception:
        logger.warning("Outbox dispatcher unavailable", exc_info=True)
    await asyncio.gather(*tasks)


# ── Standalone worker entrypoint ──────────────────────────────────────
# Run with: python -m backend.worker
# Docker: CMD ["python", "-m", "backend.worker"]
# Env: WORKER_ONLY=1 (set in Dockerfile.worker / cloudbuild.yaml)

if __name__ == "__main__":
    import os
    import sys

    # Confirm this is a standalone worker, not the API
    worker_only = os.environ.get("WORKER_ONLY", "1")
    logger.info("Starting standalone worker process (WORKER_ONLY=%s)", worker_only)

    async def _boot_and_run():
        from backend.config.settings import settings
        from backend.repositories.factory import RepositoryFactory

        # RepositoryFactory reads the shared settings module itself; it does
        # not accept a settings argument. The old call crashed before any
        # consumer could start, leaving queued automation tasks untouched.
        factory = RepositoryFactory()
        await factory.initialize()

        # Tool router (same as API init)
        from backend.mcp.tool_router import ToolRouter
        from backend.mcp.tool_policy import ToolPolicy
        from backend.services.approvals_engine import ApprovalsEngine

        approvals_engine = ApprovalsEngine()
        tool_policy = ToolPolicy()
        tool_router = ToolRouter(
            factory.mcp_repo, tool_policy, approvals_engine,
            factory.idempotency_repo, secrets_repo=factory.secrets_repo,
        )

        # Agent factory
        from backend.agents.agent_factory import AgentFactory
        from backend.services.runtime_snapshot import RuntimeSnapshotRegistry
        snapshot_registry = RuntimeSnapshotRegistry()
        agent_factory = AgentFactory(snapshot_registry, tool_router)

        # Workflow engine
        from backend.engine.engine import WorkflowEngine
        workflow_engine = WorkflowEngine(
            factory.workflow_repo, factory.message_bus,
            agent_factory, factory.memory_repo, factory.settings_repo,
            factory.notification_repo, factory.adaptive_policy_repo,
        )

        logger.info("Worker initialized. Listening for events...")
        # Phase 18: the module-level factory ref for housekeeping ticks
        # (publication recovery + memory cleanup).
        global _worker_factory
        _worker_factory = factory
        # §4.0B: the worker needs the CURRENT-authority repositories and the
        # build-waiter's mcp repo. In production a missing one is a startup
        # failure, not a silent skip of the authorization gate.
        missing = [name for name, dep in (
            ("user_repo", factory.user_repo), ("org_repo", factory.org_repo),
            ("idempotency_repo", factory.idempotency_repo),
            ("mcp_repo", getattr(factory, "mcp_repo", None))) if dep is None]
        from backend.config.settings import settings as _settings
        if missing and _settings.APP_ENV.lower() == "production":
            raise RuntimeError(
                f"Worker boot refused: required repositories missing: {missing}")
        if missing:
            logger.warning("Worker boot with missing repositories %s — "
                           "background authorization will fail closed", missing)
        from backend.services.browser_runtime_server import browser_runtime_server
        try:
            async with browser_runtime_server():
                await start_worker(factory.message_bus, workflow_engine, factory.schedule_repo,
                                   factory.token_denylist_repo, factory.audit_repo,
                                   factory.idempotency_repo, factory.user_repo, factory.org_repo,
                                   mcp_repo=getattr(factory, "mcp_repo", None))
        finally:
            from backend.services.http_pool import aclose_client
            try:
                await factory.shutdown()
            finally:
                await aclose_client()

    try:
        asyncio.run(_boot_and_run())
    except KeyboardInterrupt:
        logger.info("Worker stopped by keyboard interrupt.")
        sys.exit(0)
