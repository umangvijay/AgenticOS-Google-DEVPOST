"""Phase 9 §9.21 acceptance tests: semantic target resolution, staleness
rules, the action pipeline validator, outcome verification, progress
tracking, and the dependency lock / broker / protocol / repair services.

The DOM-level fixture journeys (reordered rows in a real browser, hydration,
virtualized tables) require the browser runtime fixture harness; the
resolution/staleness/pipeline logic is tested here through the service
boundaries that the real call chain uses.
"""
import time
from types import SimpleNamespace

import pytest

from backend.models.browser_observation import (
    ActionType, BrowserObservation, ObservedElement, validate_action_arguments,
)
from backend.models.browser_session import (
    Actor, ChallengeState, ControllerState, ResourceState,
)
from backend.services.browser_target_resolver import (
    OutcomeEvidence, OutcomeLevel, OutcomeVerdict, StalenessContext,
    TargetResolutionFailure, check_staleness, resolve_target, verify_outcome,
)
from backend.services.browser_action_validator import (
    ProgressTracker, validate_browser_action, record_milestone,
)
from backend.services.browser_runtime import ActionKind, build_action_intent


def _element(ref="e1", role="button", name="Delete", identity="INV-1042",
             container="row INV-1042", **overrides):
    e = ObservedElement(
        target_reference=ref, semantic_role=role, accessible_name=name,
        tag="button", business_identity=identity,
        semantic_container=container,
        locator_candidates=[{"kind": "role", "role": role, "name": name}])
    for k, v in overrides.items():
        setattr(e, k, v)
    return e


def _observation(elements, *, truncated=False, total=None, seq=1,
                 doc_gen=1, controller_gen=1, owner_gen=1, origin="https://app.example.com"):
    return BrowserObservation(
        observation_id="obs-1", session_id="s1", owner_generation=owner_gen,
        controller_generation=controller_gen, page_id="page-1",
        document_generation=doc_gen, observation_sequence=seq,
        captured_at=int(time.time()), sanitized_url="https://app.example.com/invoices",
        origin=origin, page_title="Invoices", elements=elements,
        elements_total=total if total is not None else len(elements),
        truncated=truncated, max_elements=120)


# ── §9.4: semantic resolution ─────────────────────────────────────────

def test_resolve_by_role_and_name():
    obs = _observation([_element("e1"), _element("e2", name="Edit", identity="INV-1043")])
    target, failure, detail = resolve_target(
        obs, role="button", accessible_name="Delete", business_identity="INV-1042")
    assert target is not None and failure is None
    assert target.element.business_identity == "INV-1042"
    assert target.strategy == "role_name"


def test_repeated_controls_require_scope():
    """§9.4: two Delete buttons (different rows) — ambiguity is REPORTED,
    never silently resolved with .first/.nth(0)."""
    obs = _observation([
        _element("e1", identity="INV-1042", container="row INV-1042"),
        _element("e2", identity="INV-1043", container="row INV-1043")])
    target, failure, detail = resolve_target(obs, role="button",
                                             accessible_name="Delete")
    assert target is None and failure is TargetResolutionFailure.TARGET_AMBIGUOUS
    # Scoping by business identity resolves uniquely (row INV-1042 → its Delete).
    target, failure, _ = resolve_target(
        obs, role="button", accessible_name="Delete", business_identity="INV-1042")
    assert target is not None and target.element.business_identity == "INV-1042"


def test_missing_target_reported_honestly():
    obs = _observation([_element("e1")])
    target, failure, _ = resolve_target(obs, target_ref="e99")
    assert target is None and failure is TargetResolutionFailure.TARGET_MISSING


def test_truncated_observation_reports_scope_not_absence():
    """§9.3 req 6: never report 'target absent' merely because it was outside
    the observed batch."""
    elements = [_element(f"e{i}") for i in range(5)]
    obs = _observation(elements, truncated=True, total=50)
    target, failure, detail = resolve_target(obs, target_ref="e42")
    assert target is None
    assert failure is None or failure is not TargetResolutionFailure.TARGET_MISSING
    # The observation itself reports remaining elements.
    assert obs.elements_remaining() == 45


# ── §9.5: staleness rules ─────────────────────────────────────────────

def _stale_ctx(**overrides):
    defaults = dict(observation_id="obs-1", document_generation=1,
                    controller_generation=1, owner_generation=1,
                    page_id="page-1", frame_id="frame-main",
                    origin="https://app.example.com",
                    target_identity="INV-1042", material_digest="m-1")
    defaults.update(overrides)
    return StalenessContext(**defaults)


def test_reordering_rows_does_not_invalidate():
    """§9.4 acceptance: reordering rows does not change which business
    object receives the action — position is not identity, so a reorder
    alone (same doc generation, same business identity) is not staleness."""
    ctx = _stale_ctx()
    failure, _ = check_staleness(ctx, _stale_ctx())
    assert failure is None


def test_navigation_invalidates():
    failure, detail = check_staleness(
        _stale_ctx(), _stale_ctx(document_generation=2))
    assert failure is TargetResolutionFailure.STALE_OBSERVATION
    failure, _ = check_staleness(_stale_ctx(), _stale_ctx(origin="https://other.io"))
    assert failure is TargetResolutionFailure.ORIGIN_CHANGED


def test_controller_change_invalidates():
    failure, _ = check_staleness(_stale_ctx(), _stale_ctx(controller_generation=2))
    assert failure is TargetResolutionFailure.CONTROL_CHANGED


def test_human_takeover_invalidates():
    failure, _ = check_staleness(_stale_ctx(), _stale_ctx(controller_is_human=True))
    assert failure is TargetResolutionFailure.CONTROL_CHANGED


def test_material_change_invalidates():
    """§9.5/§8.13: material field or resource changes invalidate."""
    failure, _ = check_staleness(_stale_ctx(), _stale_ctx(material_digest="m-2"))
    assert failure is TargetResolutionFailure.TARGET_IDENTITY_CHANGED


def test_credential_binding_change_invalidates():
    """§10.23 item 8: credential-binding changes invalidate browser context."""
    failure, detail = check_staleness(
        _stale_ctx(credential_binding_id="b-1"),
        _stale_ctx(credential_binding_id="b-2"))
    assert failure is TargetResolutionFailure.CONTROL_CHANGED
    assert "credential binding" in detail


def test_unrelated_change_does_not_invalidate():
    """§9.5: do not invalidate for unrelated changes — same doc generation,
    same controller, same origin, same identity → no staleness."""
    failure, _ = check_staleness(_stale_ctx(), _stale_ctx())
    assert failure is None


# ── §9.6: the action pipeline validator ───────────────────────────────

def test_pipeline_blocks_stale_controller():
    ok, failures = validate_browser_action(
        action_type=ActionType.CLICK, arguments={"target_ref": "e1"},
        observation_context=_stale_ctx(),
        current_context=_stale_ctx(controller_generation=2),
        session_state=ResourceState.READY, controller=ControllerState.AGENT,
        owner_epoch_matches=True, caller_is_owner=True)
    assert ok is False
    assert any("CONTROL_CHANGED" in f for f in failures)


def test_pipeline_blocks_human_controller():
    ok, failures = validate_browser_action(
        action_type=ActionType.CLICK, arguments={"target_ref": "e1"},
        observation_context=_stale_ctx(), current_context=_stale_ctx(),
        session_state=ResourceState.READY, controller=ControllerState.HUMAN,
        owner_epoch_matches=True, caller_is_owner=True)
    assert ok is False and "CONTROLLER_IS_HUMAN" in failures


def test_pipeline_blocks_unknown_arguments():
    """§9.6: the model proposes intent — it cannot inject JavaScript."""
    ok, failures = validate_browser_action(
        action_type=ActionType.CLICK,
        arguments={"target_ref": "e1", "js": "javascript:alert(1)"},
        observation_context=_stale_ctx(), current_context=_stale_ctx(),
        session_state=ResourceState.READY, controller=ControllerState.AGENT,
        owner_epoch_matches=True, caller_is_owner=True)
    assert ok is False
    assert any("unknown argument" in f for f in failures)
    assert any("javascript:" in f for f in failures)


def test_pipeline_blocks_material_approval_change():
    ok, failures = validate_browser_action(
        action_type=ActionType.CLICK, arguments={"target_ref": "e1"},
        consequential=True,
        observation_context=_stale_ctx(), current_context=_stale_ctx(),
        session_state=ResourceState.READY, controller=ControllerState.AGENT,
        owner_epoch_matches=True, caller_is_owner=True,
        approval_digest_matches=False)
    assert ok is False and "APPROVAL_MATERIAL_CHANGED" in failures


def test_pipeline_blocks_write_without_recorded_effect():
    """§8.14/§9.0D: a consequential action needs a durably recorded effect
    BEFORE dispatch."""
    ok, failures = validate_browser_action(
        action_type=ActionType.CLICK, arguments={"target_ref": "e1"},
        consequential=True,
        observation_context=_stale_ctx(), current_context=_stale_ctx(),
        session_state=ResourceState.READY, controller=ControllerState.AGENT,
        owner_epoch_matches=True, caller_is_owner=True,
        write_effect_recorded=False)
    assert ok is False and "EFFECT_NOT_RECORDED" in failures


def test_pipeline_allows_valid_action():
    ok, failures = validate_browser_action(
        action_type=ActionType.CLICK, arguments={"target_ref": "e1"},
        observation_context=_stale_ctx(), current_context=_stale_ctx(),
        session_state=ResourceState.READY, controller=ControllerState.AGENT,
        owner_epoch_matches=True, caller_is_owner=True)
    assert ok is True, failures


# ── §9.8: outcome verification ────────────────────────────────────────

def test_model_done_is_not_verification():
    """§9.8: the model saying 'done' is NOT independent verification."""
    verdict, detail = verify_outcome("click", None, model_claimed_success=True)
    assert verdict is OutcomeVerdict.INCONCLUSIVE


def test_write_without_business_evidence_is_uncertain():
    """§9.8: interaction-completed after a potentially submitted write is
    OUTCOME_UNCERTAIN — reconcile before retrying."""
    verdict, detail = verify_outcome(
        "submit", OutcomeEvidence(level=OutcomeLevel.INTERACTION_COMPLETED,
                                  detail="click dispatched"))
    assert verdict is OutcomeVerdict.OUTCOME_UNCERTAIN


def test_business_verified_and_read_evidence():
    verdict, _ = verify_outcome(
        "submit", OutcomeEvidence(level=OutcomeLevel.BUSINESS_VERIFIED,
                                  detail="INV-1042 deleted (read-back)"))
    assert verdict is OutcomeVerdict.VERIFIED
    # §10.8: a READ task (navigate) CAN complete on interaction evidence.
    verdict, _ = verify_outcome(
        "navigate", OutcomeEvidence(level=OutcomeLevel.INTERACTION_COMPLETED,
                                    detail="reached the page"))
    assert verdict is OutcomeVerdict.VERIFIED
    # §10.8: a GENERIC click with only interaction evidence is UNCERTAIN.
    verdict, _ = verify_outcome(
        "click", OutcomeEvidence(level=OutcomeLevel.INTERACTION_COMPLETED,
                                 detail="click dispatched"))
    assert verdict is OutcomeVerdict.OUTCOME_UNCERTAIN


# ── §9.17: progress tracking ──────────────────────────────────────────

def test_progress_tracker_detects_loops_not_animations():
    """§9.17: meaningful progress signals — NOT raw HTML hashing (animations
    change HTML without progress)."""
    tracker = ProgressTracker()
    # Same action, no state change, 3 times → stall detected.
    assert tracker.record_action("fp-1", changed_state=False) is None
    assert tracker.record_action("fp-1", changed_state=False) is None
    stall = tracker.record_action("fp-1", changed_state=False)
    assert stall is not None and "repeated equivalent action" in stall
    # A state-changing action resets the concern.
    assert tracker.record_action("fp-1", changed_state=True) is None
    # Milestones: a new one is progress; a repeat is not.
    milestones: list = []
    assert record_milestone(milestones, "reached invoices") is True
    assert record_milestone(milestones, "reached invoices") is False


def test_uncertain_submission_stops_the_loop():
    tracker = ProgressTracker()
    first = tracker.record_uncertain_submission()
    assert "reconcile" in first
    second = tracker.record_uncertain_submission()
    assert "repeated attempt" in second


def test_ambiguity_bounded():
    tracker = ProgressTracker()
    for _ in range(3):
        assert tracker.record_ambiguity("invoices/delete") is None
    stall = tracker.record_ambiguity("invoices/delete")
    assert stall is not None and "human control" in stall


# ── §9.6: argument schemas ────────────────────────────────────────────

def test_action_argument_schemas():
    ok, errors = validate_action_arguments(ActionType.FILL,
                                           {"target_ref": "e1", "value": "x"})
    assert ok is True
    ok, errors = validate_action_arguments(ActionType.FILL, {"target_ref": "e1"})
    assert ok is False and any("value" in e for e in errors)
    ok, errors = validate_action_arguments(
        ActionType.NAVIGATE, {"url": "https://x.io", "bogus": 1})
    assert ok is False and any("bogus" in e for e in errors)
    ok, errors = validate_action_arguments(
        ActionType.NAVIGATE, {"url": "javascript:alert(1)"})
    assert ok is False
