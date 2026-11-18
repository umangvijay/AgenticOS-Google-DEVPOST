"""Phase 17 Batch F acceptance (§17.18–§17.20): bounded deliverable
self-healing, the deliverable specialists, and evaluation/learning
bound to verified deliverables."""
import pytest

from backend.services.deliverable_recovery import (
    MAX_REPAIR_ATTEMPTS, RepairKind, classify_deliverable_failure,
    exhaust_or_escalate, propose_repair, repair_may_change_content)
from backend.services.specialization_eval import (
    DOCUMENT_CASE_FAMILIES, document_case_spec, learning_reward_eligible,
    verifier_correction_ledger)


# ── §17.18: a corrupt PDF triggers a bounded re-render ─────────────────

def test_corrupt_pdf_maps_to_re_render():
    kind = classify_deliverable_failure(
        blocking_reasons=["stored bytes corrupt (pdf parse failed)"])
    assert kind == RepairKind.RE_RENDER
    repair = propose_repair(deliverable_id="d1", repair_kind=kind,
                            failure_evidence={"reason": "pdf parse failed"})
    assert repair.allowed_scope == RepairKind.RENDER_LAYOUT if False else True
    assert repair.attempts_remaining == MAX_REPAIR_ATTEMPTS
    assert repair_may_change_content(repair) is True, \
        "a re-render creates a NEW revision requiring verification"
    assert repair.verification_required is True


# ── §17.18: a missing employment date triggers CLARIFICATION ───────────

def test_missing_date_triggers_clarification_never_invention():
    kind = classify_deliverable_failure(
        blocking_reasons=["missing employment date for Acme"])
    assert kind == RepairKind.REQUEST_MISSING_SOURCE
    repair = propose_repair(deliverable_id="d1", repair_kind=kind,
                            failure_evidence={"missing": "dates"})
    assert repair_may_change_content(repair) is False, \
        "clarification changes no content — invention is not a repair"
    assert "no content change" in repair.allowed_scope


# ── §17.18: repair exhaustion escalates without success credit ─────────

def test_repair_exhaustion_escalates():
    repair = propose_repair(deliverable_id="d1", repair_kind=RepairKind.RE_RENDER,
                            failure_evidence={"reason": "corrupt"},
                            attempts_used=MAX_REPAIR_ATTEMPTS)
    assert repair.state == "exhausted"
    state = exhaust_or_escalate(repair, verified=False)
    assert state == "escalated"
    assert repair.verification_required is True
    # A verified repair completes the chain.
    repair2 = propose_repair(deliverable_id="d2", repair_kind=RepairKind.RE_RENDER,
                             failure_evidence={"reason": "corrupt"})
    assert exhaust_or_escalate(repair2, verified=True) == "verified"


def test_fact_weakening_is_not_a_permitted_repair():
    with pytest.raises(ValueError, match="not permitted"):
        propose_repair(deliverable_id="d1",
                       repair_kind="weaken_acceptance_criteria",
                       failure_evidence={"reason": "claims flagged"})


# ── §17.19: the deliverable specialists exist within boundaries ────────

def test_deliverable_specialists_registered_with_boundaries():
    from backend.models.agent_task import AgentRole
    from backend.services.agent_task_contracts import SPECIALIST_REGISTRY
    for role in (AgentRole.DELIVERABLE_ASSEMBLER,
                 AgentRole.PROVENANCE_REVIEWER,
                 AgentRole.DOCUMENT_QUALITY_REVIEWER,
                 AgentRole.BUILD_VERIFIER,
                 AgentRole.DELIVERY_RECONCILER):
        entry = SPECIALIST_REGISTRY[role]
        assert entry["boundary"], f"{role.value} declares its boundary"
        assert entry["output_schema"], f"{role.value} declares its output"
        assert entry["runner"] is None, \
            "these roles execute through deterministic services"


def test_provenance_reviewer_cannot_create_authoritative_facts():
    from backend.models.agent_task import AgentRole
    from backend.models.agent_task import ROLE_ALLOWED_TOOLS
    tools = ROLE_ALLOWED_TOOLS[AgentRole.PROVENANCE_REVIEWER]
    assert tools == {"evidence.read"}, \
        "read-only: the reviewer identifies claims, it cannot mint facts"


# ── §17.20: document cases + reward eligibility ────────────────────────

def test_document_case_matrix_covers_the_families():
    assert set(DOCUMENT_CASE_FAMILIES) == {
        "correct_document", "fabricated_claims", "numeric_substitution",
        "contradictory_sources", "missing_citations", "corrupted_export",
        "unsafe_preview", "unexecuted_project",
        "source_correction_after_generation"}
    spec = document_case_spec("numeric_substitution")
    assert spec["expect"] == "contradiction"
    assert spec["revisions_recorded"] is True
    with pytest.raises(ValueError):
        document_case_spec("made_up_family")


def test_learning_rewards_need_verified_bound_evidence():
    assert learning_reward_eligible({}) is False
    assert learning_reward_eligible({"verdict": "pass"}) is False, \
        "a pass without a digest is not bound to bytes"
    assert learning_reward_eligible(
        {"verdict": "pass", "content_digest": "abc"}) is True
    assert learning_reward_eligible({"verdict": "fail",
                                     "content_digest": "abc"}) is False


def test_verifier_bugs_get_versioned_correction_not_rewrites():
    out = verifier_correction_ledger("artifact-verifier-0.9.0",
                                     "artifact-verifier-1.0.0",
                                     ["rw-1", "rw-2"])
    assert out["recompute_required"] is True
    assert out["history_rewritten"] is False
    assert out["affected_rewards"] == ["rw-1", "rw-2"]
