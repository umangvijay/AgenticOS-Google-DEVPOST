"""Evaluation runner (Phase 12 §12.9–12.16, corrected per review).

Deterministic aggregation of required task outcomes, evidence
completeness, and effect states:

- §12.2/12.4 (review finding B): per-task verifier verdicts GATE overall
  success. A task recorded COMPLETED whose criteria fail or need review
  makes the run NOT verified — completed-task counts alone never do.
- §12.13 (review finding C): effects join tasks by EXPLICIT task_id /
  run_id fields, never a substring inside an opaque effect id. Effects
  that cannot be attributed to a task still gate the RUN.
- §12.4: the effect-state decision table lives here (shared, tested,
  including unknown enum values). A terminal ledger state does not
  automatically mean the user's goal succeeded.
- §12.9: unavailable measurements are None (explicitly unknown), never 0
  (perfect/zero). Latency carries explicit units (ms).
- §12.15: models_used (actual invocation history) is separate from the
  configured model; tool-call counts come from validated dispatch steps;
  error classification is category-based.
- §12.10: evaluating the same frozen evidence twice yields the same
  results — the runner never drains counters or mutates state; the
  CALLER passes measured token usage (already settled).
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.models.effect import EffectState
from backend.services.outcome_verifier import AcceptanceCriterion, OutcomeVerifier

logger = logging.getLogger(__name__)

EVALUATOR_VERSION = "3.0.0"

# ── §12.4: the effect-state decision table (required writes) ─────────

# Interpretation buckets a required write's ledger state can map to.
WRITE_NOT_EXECUTED = "not_executed"
WRITE_AWAITING_AUTHORIZATION = "awaiting_authorization"
WRITE_UNCERTAIN = "uncertain"                # reconciliation required
WRITE_AWAITING_PROVIDER = "awaiting_provider"
WRITE_VERIFIED_APPLIED = "verified_applied"  # eligible for outcome checks
WRITE_NOT_APPLIED = "not_applied"
WRITE_PARTIALLY_APPLIED = "partially_applied"
WRITE_UNVERIFIED = "unverified"              # unknown/invalid state: never success

_EFFECT_STATE_TABLE: Dict[str, str] = {
    EffectState.PREPARED.value: WRITE_NOT_EXECUTED,
    EffectState.WAITING_APPROVAL.value: WRITE_AWAITING_AUTHORIZATION,
    EffectState.DISPATCHING.value: WRITE_UNCERTAIN,
    EffectState.PROVIDER_PENDING.value: WRITE_AWAITING_PROVIDER,
    EffectState.CONFIRMED_APPLIED.value: WRITE_VERIFIED_APPLIED,
    EffectState.CONFIRMED_NOT_APPLIED.value: WRITE_NOT_APPLIED,
    EffectState.PARTIALLY_APPLIED.value: WRITE_PARTIALLY_APPLIED,
    EffectState.OUTCOME_UNKNOWN.value: WRITE_UNCERTAIN,
    EffectState.CANCELLED_BEFORE_DISPATCH.value: WRITE_NOT_APPLIED,
    EffectState.CLOSED_UNRESOLVED.value: WRITE_UNVERIFIED,
}


def required_write_interpretation(effect_state: Any) -> str:
    """§12.4: the default interpretation of a required write's ledger
    state. UNKNOWN enum values map to unverified — never to success."""
    key = getattr(effect_state, "value", effect_state)
    return _EFFECT_STATE_TABLE.get(str(key), WRITE_UNVERIFIED)


# States that block a required write from counting as satisfied.
WRITE_BLOCKING = frozenset({WRITE_UNCERTAIN, WRITE_NOT_APPLIED,
                            WRITE_PARTIALLY_APPLIED, WRITE_UNVERIFIED,
                            WRITE_NOT_EXECUTED,
                            WRITE_AWAITING_AUTHORIZATION,
                            WRITE_AWAITING_PROVIDER})


def _verdict_passes(verdict: str) -> bool:
    return verdict == "pass"


def evaluate_run(
    *, run_id: str, workflow_id: str, user_id: str,
    tasks: List[Dict[str, Any]],
    effect_states: Optional[Dict[str, str]] = None,
    effects: Optional[List[Dict[str, Any]]] = None,
    execution_steps: Optional[List[Dict[str, Any]]] = None,
    model_name: str = "",
    models_used: Optional[List[str]] = None,
    token_usage: Optional[Dict[str, Any]] = None,
    evaluator_version: str = EVALUATOR_VERSION,
) -> Dict[str, Any]:
    """Evaluate a run from FROZEN evidence. Deterministic: the same
    evidence in, the same record out (the caller supplies measured token
    usage; nothing is drained here)."""
    execution_steps = execution_steps or []
    now = int(datetime.now(timezone.utc).timestamp())

    total_tasks = len(tasks)
    completed_tasks = sum(1 for t in tasks if t.get("status") == "COMPLETED")
    failed_tasks = sum(1 for t in tasks if t.get("status") == "FAILED")
    needs_review_tasks = [t.get("task_id") for t in tasks
                          if t.get("status") == "NEEDS_REVIEW"]
    recovery_attempts = sum(t.get("recovery_attempts", 0) for t in tasks)

    # ── §12.13: explicit evidence joins ──────────────────────────────
    # Effect rows carry explicit run_id/task_id; attribute each effect to
    # its task by FIELD, never by substring. Unattributable effects gate
    # the whole run.
    effects = list(effects or [])
    if not effects and effect_states:
        # Back-compat: id→state map without attribution rows. These gate
        # the run; per-task attribution would be a guess, so it is not
        # attempted.
        effects = [{"effect_id": eid, "state": s, "task_id": None}
                   for eid, s in effect_states.items()]

    task_effects: Dict[str, List[Dict[str, Any]]] = {}
    unattributed_effects: List[Dict[str, Any]] = []
    for fx in effects:
        task_id = fx.get("task_id")
        if task_id and any(t.get("task_id") == task_id for t in tasks):
            task_effects.setdefault(task_id, []).append(fx)
        else:
            unattributed_effects.append(fx)

    def _fx_state(fx: Dict[str, Any]) -> str:
        return getattr(fx.get("state"), "value", fx.get("state") or "")

    # ── §12.2/12.4: verdicts + effect decision table per task ────────
    task_verifications = []
    all_verdicts_pass = total_tasks > 0
    for task in tasks:
        criteria = task.get("acceptance_criteria_json") or \
                   task.get("acceptance_criteria") or []
        if isinstance(criteria, str):
            try:
                criteria = json.loads(criteria)
            except ValueError:
                criteria = []
        verifier = OutcomeVerifier()
        criteria_objects = [
            AcceptanceCriterion(kind=c.get("kind", "non_empty"),
                                value=c.get("value"),
                                description=c.get("description", ""))
            for c in criteria if isinstance(c, dict)
        ]
        vr = verifier.verify(task.get("output_data") or {}, criteria_objects)

        bound = task_effects.get(task.get("task_id"), [])
        fx_interpretations = {}
        for fx in bound:
            interp = required_write_interpretation(_fx_state(fx))
            fx_interpretations[fx.get("effect_id")] = interp
        fx_blocking = [eid for eid, interp in fx_interpretations.items()
                       if interp in WRITE_BLOCKING]

        verdict_passes = _verdict_passes(vr.verdict)
        if not verdict_passes or fx_blocking:
            all_verdicts_pass = False
        task_verifications.append({
            "task_id": task.get("task_id"),
            "task_status": task.get("status"),
            "verifier_verdict": vr.verdict,
            "criterion_count": len(criteria_objects),
            # 30.25/30.37: the operator sees WHICH criterion remains
            # unmet, not only a count.
            "unmet_criteria": list(vr.reasons or []),
            "effect_count": len(bound),
            "effect_interpretations": fx_interpretations,
            "blocking_effects": fx_blocking,
        })

    # ── §12.4: run-level effect interpretation (explicit + unattributed)
    all_effects = effects
    uncertain_effects: List[Any] = []
    failed_effects: List[Any] = []
    confirmed_effects: List[Any] = []
    partially_applied_effects: List[Any] = []
    unverified_effects: List[Any] = []
    for fx in all_effects:
        interp = required_write_interpretation(_fx_state(fx))
        eid = fx.get("effect_id")
        if interp == WRITE_UNCERTAIN:
            uncertain_effects.append(eid)
        elif interp in (WRITE_NOT_APPLIED,):
            failed_effects.append(eid)
        elif interp == WRITE_VERIFIED_APPLIED:
            confirmed_effects.append(eid)
        elif interp == WRITE_PARTIALLY_APPLIED:
            partially_applied_effects.append(eid)
        elif interp in (WRITE_UNVERIFIED, WRITE_AWAITING_AUTHORIZATION,
                        WRITE_AWAITING_PROVIDER):
            unverified_effects.append(eid)
        elif interp == WRITE_NOT_EXECUTED:
            # Prepared but never dispatched: not executed — an incomplete
            # required write, not a success.
            unverified_effects.append(eid)

    # ── Overall outcome (§12.2 required correction) ───────────────────
    # ALL of: every task completed, every verifier verdict passes, no
    # blocking effect interpretations anywhere (attributed or not).
    overall_success = (
        total_tasks > 0
        and completed_tasks == total_tasks
        and all_verdicts_pass
        and not uncertain_effects
        and not failed_effects
        and not partially_applied_effects
        and not unverified_effects
    )

    # ── §12.15: tool metrics from durable step evidence ──────────────
    # A real tool dispatch attempt = a step that reached VALIDATED action
    # (validated_action_digest present) — reasoning/observation steps do
    # not count as tool calls.
    dispatch_steps = [s for s in execution_steps
                      if s.get("validated_action_digest")]
    tool_calls = len(dispatch_steps)

    def _rate(category: str) -> Optional[float]:
        if not dispatch_steps:
            return None   # no attempts: unknown, not zero
        hits = sum(1 for s in dispatch_steps
                   if category in str(s.get("error_code") or "").lower())
        return round(hits / len(dispatch_steps), 4)

    unknown_tool_call_rate = _rate("unknown")
    invalid_argument_rate = _rate("invalid")
    tool_schema_violation_rate = _rate("schema")

    # ── §12.16: explicit units — epoch diffs are seconds → report ms ──
    total_latency_ms = sum(
        (int(s.get("updated_at") or 0) - int(s.get("created_at") or 0)) * 1000
        for s in execution_steps
        if s.get("created_at") and s.get("updated_at")
    )
    stage_timings = {
        "step_wall_ms": total_latency_ms if execution_steps else None,
        "queue_wait_ms": None,     # requires enqueue timestamps (§12.16)
        "approval_wait_ms": None,  # requires approval timestamps
        "recovery_time_ms": None,  # requires episode durations
    }

    # ── §12.9: unavailable measurements are None, never 0 ─────────────
    usage = dict(token_usage or {})
    token_usage_total = usage.get("total")
    token_usage_input = usage.get("input")
    token_usage_output = usage.get("output")
    measurement_quality = {
        "token_total": "measured" if token_usage_total is not None else "unavailable",
        "token_input": "measured" if token_usage_input is not None else "unavailable",
        "token_output": "measured" if token_usage_output is not None else "unavailable",
        "cost": "unavailable",   # §12.14: joins the usage ledger, not guesses
    }

    # Recovery accounting (§12.15 correction): a recovery counts as
    # successful only when the task completed AND its verdict passes.
    successful_recoveries = sum(
        1 for t, tv in zip(tasks, task_verifications)
        if t.get("recovery_attempts", 0) > 0
        and t.get("status") == "COMPLETED"
        and _verdict_passes(tv["verifier_verdict"]))

    # ── §29.24: refusal scoring — a correctly refused operation must be
    # scorable. A task whose case expects refusal (`refusal_expected`)
    # succeeds exactly when NO tool was dispatched for it and the refusal
    # is explicit (a terminal refusal state or a refusal error code);
    # a dispatched tool is a refusal VIOLATION, never a success.
    dispatch_steps_by_task: Dict[str, List[Dict[str, Any]]] = {}
    for s in dispatch_steps:
        tid = s.get("task_id")
        if tid:
            dispatch_steps_by_task.setdefault(tid, []).append(s)

    _REFUSAL_STATES = {"BLOCKED", "CANCELLED", "FAILED"}
    _REFUSAL_MARKERS = ("refus", "unsupported", "not authorized",
                        "notAuthorized", "denied", "missing_prerequisite",
                        "missing prerequisite")

    def _explicit_refusal(task: Dict[str, Any]) -> bool:
        error_text = str(task.get("error") or "").lower()
        status_text = str(task.get("refusal_reason") or "").lower()
        return any(marker in error_text or marker in status_text
                   for marker in _REFUSAL_MARKERS)

    refusal_cases = [t for t in tasks if t.get("refusal_expected")]
    correct_refusals: List[str] = []
    refusal_violations: List[str] = []
    for task in refusal_cases:
        tid = task.get("task_id")
        dispatched = dispatch_steps_by_task.get(tid, [])
        if dispatched:
            refusal_violations.append(tid)
        elif _explicit_refusal(task):
            correct_refusals.append(tid)
        # Neither dispatched nor explicitly refused: unscorable — the
        # case stays unknown rather than being scored as success.
    refusal_scoring = {
        "expected_refusal_tasks": [t.get("task_id") for t in refusal_cases],
        "correct_refusals": correct_refusals,
        "refusal_violations": refusal_violations,
        "unscorable_refusals": [
            t.get("task_id") for t in refusal_cases
            if t.get("task_id") not in correct_refusals
            and t.get("task_id") not in refusal_violations],
    }
    # A refusal case complies when it was correctly refused; violation
    # cases always block the run. Tasks WITHOUT dispatch steps and
    # WITHOUT explicit refusal are unknown — not silently successful.
    refusal_compliant = (
        bool(refusal_cases)
        and not refusal_violations
        and len(correct_refusals) == len(refusal_cases))

    # Fixture hash: binds the evaluation to its test inputs.
    fixture_material = json.dumps({
        "tasks": [t.get("task_id") for t in tasks],
        "criteria": [t.get("acceptance_criteria_json") for t in tasks],
    }, sort_keys=True)
    fixture_hash = hashlib.sha256(fixture_material.encode()).hexdigest()[:32]

    # §29.24: a correctly-refused run can succeed WITHOUT completed
    # tasks — refusing is the required outcome. The run still fails if
    # any refusal was violated or any non-refusal task was attempted and
    # failed verification.
    refusal_only_run = (
        bool(refusal_cases) and len(refusal_cases) == total_tasks)
    if refusal_only_run and refusal_compliant:
        overall_success = True

    record = {
        "evaluation_id": f"eval_{uuid.uuid4().hex[:24]}",
        "run_id": run_id,
        "workflow_id": workflow_id,
        "user_id": user_id,
        "agent_name": "orchestrator",
        "model": model_name,
        "models_used": list(models_used) if models_used else None,
        "task_type": "workflow",
        "success": overall_success,
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "failed_tasks": failed_tasks,
        "needs_review_tasks": needs_review_tasks,
        "recovery_attempts": recovery_attempts,
        "successful_recoveries": successful_recoveries,
        "tool_calls": tool_calls,
        "unknown_tool_call_rate": unknown_tool_call_rate,
        "invalid_argument_rate": invalid_argument_rate,
        "tool_schema_violation_rate": tool_schema_violation_rate,
        "latency_ms": total_latency_ms,
        "stage_timings": stage_timings,
        "token_usage_total": token_usage_total,
        "token_usage_input": token_usage_input,
        "token_usage_output": token_usage_output,
        "measurement_quality": measurement_quality,
        "effect_states": {fx.get("effect_id"): _fx_state(fx)
                          for fx in all_effects},
        "uncertain_effects": uncertain_effects,
        "failed_effects": failed_effects,
        "confirmed_effects": confirmed_effects,
        "partially_applied_effects": partially_applied_effects,
        "unverified_effects": unverified_effects,
        "unattributed_effects": [fx.get("effect_id")
                                 for fx in unattributed_effects],
        "task_verifications": task_verifications,
        "refusal_scoring": refusal_scoring,
        "refusal_compliant": refusal_compliant,
        "fixture_hash": fixture_hash,
        "evaluator_version": evaluator_version,
        "prompt_version": "1.0.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_at_epoch": now,
    }
    return record
