import logging
from backend.repositories.message_bus import MessageBus
from backend.repositories.workflow_repository import WorkflowRepository
from backend.repositories.audit_repository import audit_repo, AuditEvent
from backend.models.schemas import TaskRecoveryEvent, TaskStatus, TaskTriggerEvent, SemanticErrorReason
from backend.models.recovery import RecoveryContext, RecoveryAction, RecoveryActionEnum
from backend.agents.recovery.recovery_agent import get_recovery_agent
from backend.engine.repo_adapter import load_run, persist_task
from datetime import datetime, timezone

logger = logging.getLogger("recovery_worker")

class RecoveryWorker:
    def __init__(self, message_bus: MessageBus, workflow_repo: WorkflowRepository, workflow_engine=None):
        self.message_bus = message_bus
        self.repo = workflow_repo
        self.workflow_engine = workflow_engine

    async def handle_recovery_event(self, event, ctx=None):
        """Processes a TaskRecoveryEvent from the message bus."""
        try:
            if not isinstance(event, TaskRecoveryEvent):
                event = TaskRecoveryEvent(**event)
            await self._recover(event)
            if ctx:
                await ctx.ack()
        except Exception as e:
            logger.error(f"Recovery worker failed to process event: {e}")
            if ctx:
                await ctx.nack()

    async def _claim_recovery_episode(self, run, task):
        """§12.5: the durable recovery EPISODE is the claim authority —
        atomic get-or-create (dedup for the same failure episode) + CAS
        ownership claim with lease + generation. Competing workers and
        redelivery cannot start duplicate logical recoveries; the task
        counter below remains a SUMMARY, never the authorization."""
        from backend.models.admission import epoch_now
        from backend.repositories.recovery_episode_repository import (
            get_recovery_episode_repo, new_episode_id,
        )
        repo = get_recovery_episode_repo()
        if repo is None:
            logger.warning(
                "Recovery episode repository unavailable; refusing to "
                "authorize recovery from the task counter (§12.5)")
            return None
        episode = await repo.get_episode_for_task(run.run_id, task.task_id)
        if episode is None:
            import hashlib as _hashlib
            episode = await repo.create_episode({
                "episode_id": new_episode_id(),
                "tenant_id": run.user_id,
                "actor_id": "system",
                "run_id": run.run_id,
                "task_id": task.task_id,
                "failure_code": str(getattr(task, "error_type", "") or
                                    "UNKNOWN")[:60],
                "goal_digest": _hashlib.sha256(
                    str(task.original_input or task.input_data).encode()
                ).hexdigest()[:16],
                "acceptance_criteria": list(
                    getattr(task, "acceptance_criteria", None) or []),
                # §12.5: persistent ABSOLUTE deadline from creation.
                "deadline_at": epoch_now() + 900,
            })
        from backend.services.capacity_service import WORKER_ID
        claimed = await repo.claim_episode(episode["episode_id"], WORKER_ID,
                                           lease_seconds=120)
        if claimed is None:
            logger.info(
                "Recovery episode %s already owned by another live worker; "
                "duplicate/stale delivery ignored", episode["episode_id"])
        return claimed

    async def _recover(self, event: TaskRecoveryEvent):
        run = await load_run(self.repo, event.run_id)
        if not run:
            return

        task = next((t for t in run.tasks if t.task_id == event.task_id), None)
        if not task:
            return

        # Fast-path duplicate guard (cheap, event-level): only recover if
        # status is RECOVERING and attempts match. The EPISODE claim below
        # is the actual authorization.
        if task.status != TaskStatus.RECOVERING or task.recovery_attempts + 1 != event.recovery_attempt:
            logger.info(f"Ignoring duplicate/stale recovery event for {task.task_id}")
            return

        # §12.5: claim the durable episode — competing workers resolve to
        # ONE owner with a live lease + generation.
        episode = await self._claim_recovery_episode(run, task)
        if episode is None:
            return

        from backend.models.admission import epoch_now
        deadline_at = episode.get("deadline_at")
        if deadline_at and epoch_now() > int(deadline_at):
            # §12.5: the persistent absolute deadline is active — escalate
            # instead of recovering past it.
            from backend.repositories.recovery_episode_repository import (
                get_recovery_episode_repo, RecoveryState,
            )
            ep_repo = get_recovery_episode_repo()
            if ep_repo is not None:
                try:
                    await ep_repo.transition(
                        episode["episode_id"], RecoveryState.ESCALATED,
                        actor="system",
                        expected_owner_generation=episode.get("owner_generation"),
                        require_live_lease=True,
                        error_code="RECOVERY_DEADLINE_EXCEEDED")
                except Exception:
                    logger.debug("Episode escalation transition failed",
                                 exc_info=True)
            task.recovery_attempts += 1
            task.status = TaskStatus.FAILED
            task.error = "[recovery-deadline-exceeded] recovery deadline passed"
            task.completed_at = datetime.now(timezone.utc)
            await persist_task(self.repo, run.run_id, task)
            await self._trigger_dag(run.run_id)
            return

        # Summary counter (NOT the authorization): the episode claim above
        # decided ownership.
        task.recovery_attempts += 1
        await persist_task(self.repo, run.run_id, task)

        # Formal recovery policy gate (v5.6 §3): deterministic caps, deadline,
        # and repeated-action detection decide whether the LLM recovery may
        # run at all. Refusals escalate into a structured incident.
        from backend.engine.failure_classifier import classify_failure
        from backend.services.recovery_policy import (
            RecoveryAttempt, RecoveryPolicy,
        )
        classifier = classify_failure(task.error or "")
        # §15.17 (audit W3): the SPECIALIST failure is classified through
        # the response table BEFORE a strategy is chosen — the permitted
        # response travels with the episode (an uncertain external write
        # reconciles; it is never replayed blind).
        try:
            from backend.services.specialist_recovery import (
                classify_specialist_failure,
            )
            _spec = classify_specialist_failure(
                error_code=classifier.category,
                error_text=task.error or "",
                attempts=(task.recovery_attempts or 0) + 1)
            episode["specialist_failure_response"] = {
                "failure_class": _spec.failure_class.value,
                "permitted_response": _spec.response.value,
                "may_reexecute": _spec.may_reexecute,
                "detail": _spec.detail,
            }
        except Exception:
            logger.debug("Specialist failure classification skipped",
                         exc_info=True)
        # §12.5: the episode records the classification durably.
        from backend.repositories.recovery_episode_repository import (
            get_recovery_episode_repo, RecoveryState,
        )
        ep_repo = get_recovery_episode_repo()
        if ep_repo is not None:
            try:
                await ep_repo.transition(
                    episode["episode_id"], RecoveryState.CLASSIFIED,
                    actor="system", error_code=classifier.category)
            except Exception:
                logger.debug("Episode CLASSIFIED transition skipped",
                             exc_info=True)
        # §11.0 Finding 9: cycle detection uses the ACTUAL historical
        # repair fingerprints (corrected arguments), not reconstructed
        # hashes from the current category + original inputs.
        prior = []
        for i, h in enumerate(getattr(task, "recovery_history", []) or []):
            h_action = h.get("action") or {}
            h_input = h_action.get("corrected_input") or task.input_data
            prior.append(RecoveryAttempt(
                attempt=i + 1,
                category=h_action.get("failure_category", classifier.category),
                action=h_action.get("action", "repair"),
                action_hash=RecoveryPolicy.action_hash(
                    task.tool or task.agent,
                    h_action.get("failure_category", classifier.category),
                    h_input),
                rationale=str(h_action.get("rationale", ""))[:120],
                verified=h_action.get("verified", False),
            ))
        # §11.0 Finding 7: pass the recovery start time so the deadline
        # is enforced (the policy treats a missing start time as "not exceeded").
        recovery_started_at = None
        if getattr(task, "recovery_history", None):
            first = task.recovery_history[0]
            recovery_started_at = first.get("timestamp")
        policy = RecoveryPolicy(attempts=prior,
                                recovery_started_at=recovery_started_at)
        decision = policy.decide(classifier, task.tool or task.agent,
                                 task.original_input or task.input_data)
        if not decision.allowed:
            from backend.services.notification_service import NotificationService
            incident = RecoveryPolicy.incident(
                task.task_id, run.run_id, prior, classifier, decision.reason)
            task.status = TaskStatus.FAILED
            task.error = f"[recovery-escalated] {decision.reason}"
            await persist_task(self.repo, run.run_id, task)
            # §12.5: the episode records the escalation too.
            if ep_repo is not None:
                try:
                    await ep_repo.transition(
                        episode["episode_id"], RecoveryState.ESCALATED,
                        actor="system",
                        expected_owner_generation=episode.get("owner_generation"),
                        require_live_lease=True,
                        error_code=f"policy:{decision.reason}"[:120])
                except Exception:
                    logger.debug("Episode ESCALATED transition failed",
                                 exc_info=True)
            try:
                await NotificationService(getattr(self.workflow_engine, "notification_repo", None) if self.workflow_engine else None).create(
                    user_id=run.user_id,
                    notification_type="SYSTEM",
                    title="Automatic recovery stopped — action needed",
                    body=decision.reason,
                    metadata={"incident": incident, "run_id": run.run_id, "task_id": task.task_id},
                )
            except Exception:
                logger.debug("Recovery escalation notification failed", exc_info=True)
            await self._trigger_dag(run.run_id)
            return

        try:
            if self.workflow_engine:
                from backend.services.llm_context import load_user_llm_keys, load_user_model
                factory = getattr(self.workflow_engine, "agent_factory", None)
                router = getattr(factory, "tool_router", None)
                await load_user_llm_keys(getattr(router, "secrets_repo", None), run.user_id, run_id=run.run_id)
                await load_user_model(getattr(self.workflow_engine, "settings_repo", None), run.user_id, task.input_data.get("_model"))
            context = RecoveryContext(
                workflow_id=run.workflow_id,
                run_id=run.run_id,
                task_id=task.task_id,
                task_type=task.tool or task.agent,
                original_input=task.original_input or task.input_data,
                current_input=task.input_data,
                validation_error=task.error or "Unknown semantic error",
                error_reason=getattr(SemanticErrorReason, task.error_type, SemanticErrorReason.TOOL_SEMANTIC_REJECTION),
                allowed_tool_schema={},
                previous_recovery_attempts=task.recovery_attempts - 1,
                safe_error_details="Semantic violation occurred during execution."
            )

            from backend.services import gemini_client

            prompt = f"Please analyze this recovery context and provide a RecoveryAction:\n{context.model_dump_json()}"
            events = await gemini_client.run_adk_debug(
                lambda model: get_recovery_agent(model=model),
                prompt,
                "AgentOS",
            )

            if not events:
                raise ValueError("RecoveryAgent returned empty response")

            raw_output = None
            for e in reversed(events):
                if getattr(e, 'output', None) is not None:
                    raw_output = e.output
                    break
                if hasattr(e, 'content') and e.content and hasattr(e.content, 'parts'):
                    for part in e.content.parts:
                        if hasattr(part, 'text') and part.text:
                            raw_output = part.text
                            break
                    if raw_output:
                        break

            if isinstance(raw_output, str):
                action = RecoveryAction.model_validate_json(raw_output)
            elif isinstance(raw_output, RecoveryAction):
                action = raw_output
            else:
                data = raw_output.model_dump() if hasattr(raw_output, 'model_dump') else raw_output
                action = RecoveryAction.model_validate(data)

            # §11.0 Finding 8: strict action validation — unknown actions
            # are an explicit bounded rejection, never a silent fall-through.
            if action.action not in (RecoveryActionEnum.ABORT, RecoveryActionEnum.REPAIR):
                task.status = TaskStatus.FAILED
                task.error = f"RecoveryAgent proposed unsupported action: {action.action}"
                task.completed_at = datetime.now(timezone.utc)
                await persist_task(self.repo, run.run_id, task)
                if ep_repo is not None:
                    try:
                        await ep_repo.transition(
                            episode["episode_id"], RecoveryState.RECOVERY_FAILED,
                            actor="system",
                            expected_owner_generation=episode.get("owner_generation"),
                            require_live_lease=True,
                            error_code="unsupported_action")
                    except Exception:
                        logger.debug("Episode RECOVERY_FAILED transition failed",
                                     exc_info=True)
                await self._trigger_dag(run.run_id)
                return
            if action.action == RecoveryActionEnum.REPAIR and not action.corrected_input:
                task.status = TaskStatus.FAILED
                task.error = "RecoveryAgent proposed REPAIR without corrected_input"
                task.completed_at = datetime.now(timezone.utc)
                await persist_task(self.repo, run.run_id, task)
                await self._trigger_dag(run.run_id)
                return

            if action.action == RecoveryActionEnum.ABORT:
                logger.info(f"RecoveryAgent elected to ABORT task {task.task_id}")
                task.status = TaskStatus.FAILED
                task.error = f"Unrecoverable: {action.rationale}"
                task.completed_at = datetime.now(timezone.utc)
                await persist_task(self.repo, run.run_id, task)
                await self._trigger_dag(run.run_id)
                return

            if action.action == RecoveryActionEnum.REPAIR:
                logger.info(f"RecoveryAgent provided repair for task {task.task_id}")

                # Invalidate any approvals if the input changed!
                if task.input_data.get("_approved_request_id") and action.corrected_input != task.input_data:
                    logger.warning(f"Task {task.task_id} had an approval, but arguments changed. Invalidating approval.")
                    task.input_data.pop("_approved_request_id", None)

                internal_metadata = {k: v for k, v in task.input_data.items() if k in ("_model", "_agent_id")}
                task.input_data = {k: v for k, v in (action.corrected_input or {}).items() if not k.startswith("_")}
                task.input_data.update(internal_metadata)
                task.recovery_history.append({
                    "attempt": task.recovery_attempts,
                    "action": action.model_dump(),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
                task.status = TaskStatus.PENDING
                await persist_task(self.repo, run.run_id, task)

                # §12.5: the episode records the repair PROPOSAL — the
                # recovery is NOT verified yet (§12.26: a queued correction
                # is never reported as a successful recovery). The episode
                # stays non-terminal until the recovered task's outcome is
                # verified.
                if ep_repo is not None:
                    try:
                        await ep_repo.transition(
                            episode["episode_id"], RecoveryState.PROPOSED,
                            actor="agent",
                            expected_owner_generation=episode.get("owner_generation"),
                            require_live_lease=True,
                            evidence_reference=f"repair:{action.rationale}"[:160])
                    except Exception:
                        logger.debug("Episode PROPOSED transition skipped",
                                     exc_info=True)
                    # §29.23: the worker's phase is DONE — it releases
                    # ownership so the verifier that checks the repaired
                    # outcome can acquire the lease (controlled
                    # reacquisition) and commit the verdict.
                    try:
                        await ep_repo.release_ownership(
                            episode["episode_id"],
                            owner_worker_id=episode.get("owner_worker_id") or "",
                            owner_generation=episode.get("owner_generation") or 0)
                    except Exception:
                        logger.debug("Episode ownership release skipped",
                                     exc_info=True)

                from backend.repositories.audit_repository import ActorType
                audit_repo.log_event(AuditEvent(
                    event_type="TASK_REPAIR_QUEUED",
                    actor_id="SYSTEM",
                    actor_type=ActorType.SYSTEM,
                    resource_id=task.task_id,
                    workflow_id=run.workflow_id,
                    run_id=run.run_id,
                    task_id=task.task_id,
                    details={"attempt": task.recovery_attempts, "rationale": action.rationale, "note": "repair queued; verification pending (§11.0 Finding 11)"}
                ))

                trigger_event = TaskTriggerEvent(
                    workflow_id=run.workflow_id,
                    run_id=run.run_id,
                    task_id=task.task_id
                )
                await self.message_bus.publish("agentos-workflow-events", trigger_event)

        except Exception as e:
            logger.error(f"Recovery Worker failed for task {task.task_id}: {e}")
            task.status = TaskStatus.FAILED
            task.error = f"Recovery failed: {str(e)}"
            task.completed_at = datetime.now(timezone.utc)
            await persist_task(self.repo, run.run_id, task)
            await self._trigger_dag(run.run_id)

    async def _trigger_dag(self, run_id: str):
        if self.workflow_engine:
            await self.workflow_engine.evaluate_dag(run_id)
