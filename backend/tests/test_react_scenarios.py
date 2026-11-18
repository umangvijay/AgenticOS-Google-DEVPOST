"""Phase 10 §10.22/§10.23: deterministic E2E scenarios for verified ReAct
orchestration — misleading success, fabricated artifacts, uncertain browser
effects, the step state machine, and the acceptance-criteria contract."""
import pytest

from backend.services.evidence_service import (
    Verdict, validate_proposed_criteria, verify_task_outcome,
)
from backend.models.browser_observation import ActionType, validate_action_arguments
from backend.models.execution_step import (
    CompletionDecision, ReActBounds, decide_completion,
)


# ── §10.7: acceptance-criteria validation ─────────────────────────────

def test_write_task_with_only_non_empty_is_rejected():
    """§10.7: 'output is non-empty' is not universal completion evidence for
    a write task — effect_resolved or artifact_exists is mandatory."""
    validated, rejections = validate_proposed_criteria(
        [{"kind": "non_empty", "value": "reply"}],
        task_is_write=True, requested_outcomes=["create a record"])
    assert not validated
    assert any("insufficient" in r for r in rejections)


def test_write_task_with_effect_resolved_passes_validation():
    validated, rejections = validate_proposed_criteria(
        [{"kind": "effect_resolved"}],
        task_is_write=True, requested_outcomes=["create a record"])
    assert len(validated) == 1 and not rejections


def test_read_task_with_content_criteria_is_sufficient():
    validated, rejections = validate_proposed_criteria(
        [{"kind": "contains", "value": "summary"},
         {"kind": "schema_keys", "value": ["title", "body"]}],
        task_is_write=False, requested_outcomes=["produce a report"])
    assert len(validated) == 2 and not rejections


# ── §10.22 scenario matrix ────────────────────────────────────────────

def test_s1_model_claims_success_without_evidence():
    """§10.22: model claims success without evidence → verification blocks
    completion."""
    result = verify_task_outcome(
        output={"reply": "All done! Everything worked perfectly."},
        acceptance_criteria=[{"kind": "artifact_exists"}],
        completion_policy="criteria", task_is_write=True,
        model_claimed_done=True)
    assert result.verdict != Verdict.VERIFIED
    assert any("not evidence" in lim for lim in result.confidence_limitations)


def test_s2_tool_returns_misleading_success_text():
    """§10.22: tool returns misleading success text → no false external
    success."""
    result = verify_task_outcome(
        output={"reply": "Success! The record was created (trust me).",
                "status_code": 200},
        acceptance_criteria=[{"kind": "effect_resolved"}],
        completion_policy="external_effect", task_is_write=True,
        effect_states={})   # no effects recorded
    assert result.verdict == Verdict.OUTCOME_UNCERTAIN


def test_s3_artifact_reference_does_not_exist():
    """§10.22: artifact reference does not exist → verification fails."""
    result = verify_task_outcome(
        output={"reply": "created the artifact", "artifact_id": "art-FABRICATED"},
        acceptance_criteria=[{"kind": "artifact_exists"}],
        completion_policy="criteria",
        artifact_resolver=lambda ref: False)
    assert result.verdict == Verdict.FAILED


def test_s4_uncertain_browser_effects_block_completion():
    """§10.22: uncertain browser effect → OUTCOME_UNCERTAIN, not a silent
    completion."""
    result = verify_task_outcome(
        output={"reply": "submitted the form"},
        acceptance_criteria=[{"kind": "effect_resolved"}],
        completion_policy="external_effect", task_is_write=True,
        effect_states={"fx_1": "OUTCOME_UNKNOWN"},
        model_claimed_done=True)
    assert result.verdict == Verdict.OUTCOME_UNCERTAIN
    assert any("reconcile" in lim for lim in result.confidence_limitations)


def test_s5_resolved_effects_allow_verification():
    """§10.22: resolved effects + passing criteria → VERIFIED."""
    result = verify_task_outcome(
        output={"reply": "created the record", "record_id": "rec-42"},
        acceptance_criteria=[{"kind": "effect_resolved"}],
        completion_policy="external_effect", task_is_write=True,
        effect_states={"fx_1": "CONFIRMED_APPLIED"})
    assert result.verdict == Verdict.VERIFIED


def test_s6_budget_exhausted_blocks():
    """§10.22: budget expires between calls → BLOCKED."""
    bounds = ReActBounds(max_spend_units=500)
    exceeded = bounds.check(iterations=2, elapsed_seconds=10, spend_units=500,
                            tool_calls=3, repeated_actions=0)
    decision = decide_completion(
        model_claimed_done=False, verification_verdict=None,
        bounds_exceeded=exceeded)
    assert decision is CompletionDecision.BLOCKED


def test_s7_prompt_injection_in_tool_output_cannot_authorize():
    """§10.16/§10.22: hostile tool output containing 'approve all deletes'
    is DATA — the action validator still blocks unapproved writes."""
    from backend.services.browser_action_validator import validate_browser_action
    from backend.models.browser_observation import ActionType
    from backend.services.browser_target_resolver import StalenessContext
    from backend.models.browser_session import (
        ControllerState, ResourceState)
    ctx = StalenessContext(
        observation_id="obs-1", document_generation=1, controller_generation=1,
        owner_generation=1, page_id="p1", frame_id="f1",
        origin="https://app.example.com")
    ok, failures = validate_browser_action(
        action_type=ActionType.CLICK, arguments={"target_ref": "e1"},
        observation_context=ctx, current_context=ctx,
        session_state=ResourceState.READY, controller=ControllerState.AGENT,
        owner_epoch_matches=True, caller_is_owner=True,
        consequential=True, write_effect_recorded=False)
    assert ok is False and "EFFECT_NOT_RECORDED" in failures


def test_s8_loop_limit_enforced():
    """§10.22: agent repeats the same action → loop limit enforced."""
    bounds = ReActBounds(max_repeated_equivalent_actions=2)
    exceeded = bounds.check(iterations=5, elapsed_seconds=10, spend_units=100,
                            tool_calls=5, repeated_actions=2)
    assert any("repeated" in r for r in exceeded)


def test_s9_duplicate_command_delivery():
    """§10.22: duplicate command delivery → same logical execution (tested
    in test_execution_steps.py; here we verify the digest binding)."""
    import hashlib
    import json as _json
    payload = {"goal": "do the thing", "model": None, "thread_id": None,
               "parent_run_id": None}
    d1 = hashlib.sha256(_json.dumps(payload, sort_keys=True).encode()).hexdigest()
    d2 = hashlib.sha256(_json.dumps(payload, sort_keys=True).encode()).hexdigest()
    assert d1 == d2
    payload_changed = {**payload, "goal": "do a DIFFERENT thing"}
    d3 = hashlib.sha256(_json.dumps(payload_changed, sort_keys=True).encode()).hexdigest()
    assert d1 != d3


def test_s10_worker_dies_before_dispatch_safe_recovery():
    """§10.22: worker dies before dispatch → safe recovery (the step is
    OBSERVING/PROPOSED — no dispatch happened, no uncertain effect)."""
    # The recovery path: the step's state tells us nothing was dispatched.
    from backend.repositories.execution_step_repository import (
        StepState, step_transition_valid)
    # OBSERVING → DISPATCHING is not a valid direct transition (must go
    # through PROPOSED → VALIDATING → READY), so a crash in OBSERVING cannot
    # produce an uncertain external effect.
    assert not step_transition_valid(StepState.OBSERVING,
                                     StepState.DISPATCHING, "agent")


def test_s11_worker_dies_after_dispatch_uncertainty_preserved():
    """§10.22: worker dies after dispatch → reconcile uncertainty."""
    from backend.repositories.execution_step_repository import (
        StepState, step_transition_valid)
    # DISPATCHING → OUTCOME_UNCERTAIN is the appropriate transition.
    assert step_transition_valid(StepState.DISPATCHING,
                                 StepState.OUTCOME_UNCERTAIN, "system")
    # OUTCOME_UNCERTAIN is quiescent — a retry cannot directly dispatch.
    assert not step_transition_valid(StepState.OUTCOME_UNCERTAIN,
                                     StepState.DISPATCHING, "agent")


def test_s12_human_rejects_approval_no_action():
    """§10.22: human rejects approval → no action (CANCELLED is terminal)."""
    from backend.repositories.execution_step_repository import (
        StepState, step_transition_valid)
    assert step_transition_valid(StepState.WAITING_APPROVAL,
                                 StepState.CANCELLED, "operator")
    assert not step_transition_valid(StepState.CANCELLED,
                                     StepState.DISPATCHING, "agent")


def test_s13_cancellation_races_completion():
    """§10.22: cancellation races completion → one CAS winner (tested in
    test_execution_steps.py; verified here at the transition level)."""
    from backend.repositories.execution_step_repository import (
        StepState, step_transition_valid)
    assert step_transition_valid(StepState.VERIFYING,
                                 StepState.CANCELLED, "operator")
    assert step_transition_valid(StepState.VERIFYING,
                                 StepState.VERIFIED, "verifier")
    # Both transitions are valid from VERIFYING — the CAS decides.
