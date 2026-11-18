"""Legacy EvaluationService — Phase 12 §12.9–12.14, corrected per review.

Thin adapter over the Phase 12 evaluation runner and the durable
evaluation repository. Corrections this version carries:

- §12.10: evidence COLLECTION (this adapter, authorized durable reads) is
  separate from SCORING (the pure runner). The runner never drains
  counters, mutates workflow state, or contacts external systems — the
  same frozen evidence twice yields the same results.
- §12.13: effects join tasks by EXPLICIT task_id/run_id fields (the
  durable rows carry them); nothing is inferred from opaque ids.
- §12.14: token usage comes from the DURABLE usage ledger
  (`usage_summary_for_run` — measured/estimated/uncertain with the
  actual model history). The process-local counter is never drained
  here; unavailable usage stays None (unknown ≠ zero, §12.9).
- §12.11/§12.12: duplicate terminal deliveries return the ORIGINAL
  record (create-or-get by run + scorer version); a new scorer version
  creates a linked revision. Records are never rewritten in place.
"""
import asyncio
import logging
from typing import Any, Dict, List

from backend.models.evaluation import AgentEvaluationRecord
from backend.models.schemas import WorkflowRun, TaskStatus

logger = logging.getLogger("evaluation_service")


def _task_dict(task) -> Dict[str, Any]:
    """Project a typed Task model onto the runner's task-dict shape."""
    criteria = getattr(task, "acceptance_criteria", None)
    if criteria is None:
        criteria: Any = []
    if isinstance(criteria, str):
        criteria_out: Any = criteria
    else:
        criteria_out = [
            c if isinstance(c, dict) else
            {"kind": getattr(c, "kind", "non_empty"),
             "value": getattr(c, "value", None),
             "description": getattr(c, "description", "")}
            for c in criteria
        ]
    return {
        "task_id": task.task_id,
        "status": task.status.value if isinstance(task.status, TaskStatus)
                  else str(task.status),
        "recovery_attempts": getattr(task, "recovery_attempts", 0) or 0,
        "acceptance_criteria_json": criteria_out,
        "output_data": getattr(task, "output_data", None) or {},
        "started_at": getattr(task, "started_at", None),
        "completed_at": getattr(task, "completed_at", None),
    }


async def _gather_run_evidence(run: WorkflowRun) -> Dict[str, Any]:
    """§12.10: collect authorized durable evidence. Collection failures
    are FIRST-CLASS (§12.18): an unavailable repository yields an explicit
    `unavailable` marker — never a silent empty set that could resemble a
    harmless 'no effects required'."""
    evidence: Dict[str, Any] = {
        "effects": [],
        "execution_steps": [],
        "evidence_gaps": [],
    }
    try:
        from backend.repositories.effect_repository import get_effect_repo
        effect_repo = get_effect_repo()
        if effect_repo is None:
            evidence["evidence_gaps"].append("effect_repository_unavailable")
        else:
            for fx in await effect_repo.effects_for_run(run.run_id):
                evidence["effects"].append({
                    "effect_id": fx.effect_id,
                    "task_id": fx.task_id,       # explicit join field (§12.13)
                    "run_id": fx.run_id,
                    "state": fx.state.value,
                    "operation_name": fx.operation_name,
                })
    except Exception:
        evidence["evidence_gaps"].append("effect_collection_failed")
        logger.debug("Effect evidence unavailable for %s", run.run_id,
                     exc_info=True)
    try:
        from backend.repositories.execution_step_repository import (
            get_execution_step_repo)
        step_repo = get_execution_step_repo()
        if step_repo is None:
            evidence["evidence_gaps"].append("step_repository_unavailable")
        else:
            evidence["execution_steps"] = await step_repo.steps_for_run(run.run_id)
    except Exception:
        evidence["evidence_gaps"].append("step_collection_failed")
        logger.debug("Step evidence unavailable for %s", run.run_id,
                     exc_info=True)
    return evidence


async def _gather_usage(run: WorkflowRun) -> Dict[str, Any]:
    """§12.14: authoritative usage from the durable ledger. Unavailable
    stays None — never a fabricated zero."""
    try:
        from backend.repositories.usage_repository import get_usage_repo
        usage_repo = get_usage_repo()
        if usage_repo is None or not hasattr(usage_repo, "usage_summary_for_run"):
            return {}
        return await usage_repo.usage_summary_for_run(run.run_id)
    except Exception:
        logger.debug("Usage ledger unavailable for %s", run.run_id,
                     exc_info=True)
        return {}


def _record_from_raw(raw: Dict[str, Any]) -> AgentEvaluationRecord:
    return AgentEvaluationRecord(
        evaluation_id=raw["evaluation_id"],
        run_id=raw["run_id"],
        workflow_id=raw["workflow_id"],
        agent_name=raw["agent_name"],
        model=raw["model"],
        task_type=raw["task_type"],
        success=raw["success"],
        recovery_attempts=raw["recovery_attempts"],
        successful_recoveries=raw.get("successful_recoveries", 0),
        tool_calls=raw["tool_calls"],
        unknown_tool_call_rate=raw.get("unknown_tool_call_rate"),
        invalid_argument_rate=raw.get("invalid_argument_rate"),
        tool_schema_violation_rate=raw.get("tool_schema_violation_rate"),
        latency_ms=raw.get("latency_ms"),
        token_usage_input=raw.get("token_usage_input"),
        token_usage_output=raw.get("token_usage_output"),
        token_usage_total=raw.get("token_usage_total"),
        evaluator_version=raw["evaluator_version"],
        prompt_version=raw.get("prompt_version", "1.0.0"),
        created_at=raw["created_at"],
    )


async def evaluate_run_async(run: WorkflowRun,
                             db_client=None) -> AgentEvaluationRecord:
    """Async core: collect frozen evidence, score deterministically,
    persist create-or-get (duplicate deliveries return the ORIGINAL)."""
    from backend.services.evaluation_runner import evaluate_run
    from backend.config.settings import settings

    evidence = await _gather_run_evidence(run)
    usage = await _gather_usage(run)

    token_usage = {
        # §12.14: charged-or-likely units (measured + estimated +
        # uncertain); None when the ledger holds nothing for the run.
        "total": (usage.get("total_units")
                  if usage.get("reservation_count") else None),
        "input": None,     # per-direction split lives on settlements (§12.14
        "output": None,    # — surfaced once normalized receipts are joined)
    }
    raw = evaluate_run(
        run_id=run.run_id,
        workflow_id=run.workflow_id,
        user_id=run.user_id,
        tasks=[_task_dict(t) for t in run.tasks],
        effects=evidence["effects"],
        execution_steps=evidence["execution_steps"],
        models_used=usage.get("models_used") or None,
        token_usage=token_usage,
        model_name=settings.GEMINI_MODEL,
    )
    raw["evidence_gaps"] = evidence["evidence_gaps"]
    raw["usage_summary"] = {
        k: usage.get(k) for k in ("measured_units", "estimated_units",
                                  "reserved_units", "uncertain_units",
                                  "total_units", "pricing_version",
                                  "measurement_source", "reservation_count")
    } if usage else {}
    raw["successful_recoveries"] = raw.get("successful_recoveries", 0)

    # Durable create-or-get — duplicate terminal events return the ORIGINAL.
    saved = False
    stored: Dict[str, Any] = raw
    try:
        from backend.repositories.evaluation_repository import (
            get_evaluation_repo)
        eval_repo = get_evaluation_repo()
        if eval_repo is not None:
            stored, created = await eval_repo.save_evaluation(raw)
            saved = True
            if not created:
                logger.info(
                    "Evaluation for run %s already recorded (%s); returning "
                    "the original (§12.11 duplicate delivery)",
                    run.run_id, stored.get("evaluation_id"))
    except Exception:
        logger.error("Durable evaluation save failed for run %s",
                     run.run_id, exc_info=True)
    if not saved and db_client is not None:
        # Legacy Firestore path (pre-Phase-12 deployments).
        try:
            db_client.collection("agent_evaluations").document(
                raw["evaluation_id"]).set(raw)
        except Exception:
            logger.error("Firestore evaluation save failed for run %s",
                         run.run_id, exc_info=True)
    return _record_from_raw(stored)


class EvaluationService:
    """Backward-compatible facade. `evaluate_run` remains synchronous for
    existing callers; inside a live event loop callers MUST await
    `evaluate_run_async` (a double compute would double-score)."""

    def __init__(self, db_client=None):
        self.db = db_client
        self.evaluations: List[AgentEvaluationRecord] = []

    def evaluate_run(self, run: WorkflowRun) -> AgentEvaluationRecord:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "EvaluationService.evaluate_run cannot run inside a live "
                "event loop — await evaluate_run_async(run) instead")
        # No running loop (tests, CLI): run to completion.
        record = asyncio.run(evaluate_run_async(run, self.db))
        self.evaluations.append(record)
        return record
