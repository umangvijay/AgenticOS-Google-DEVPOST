"""Phase 14 §14.0–§14.13 acceptance rows: the evidence register, the
release scope, per-category measurements, the action catalog +
eligibility filter, the context schema, the decision record, and the
ONE-selector structural guarantee."""
import json

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def db(tmp_path):
    d = DatabaseManager(str(tmp_path / "foundation.db"))
    await d.initialize()
    try:
        yield d
    finally:
        await d.close()


# ── §14.0: the evidence register ───────────────────────────────────────

@pytest.mark.asyncio
async def test_evidence_register_refuses_unevidenced_claims(db):
    from backend.services.evidence_register import (
        EvidenceRegister, EvidenceRegisterError)
    reg = EvidenceRegister(db)
    with pytest.raises(EvidenceRegisterError, match="requires a test reference"):
        await reg.record(subphase_id="14.x", claim="done",
                         status="complete", evidence_level="verified_locally")
    # WITH the test reference, the claim records and is queryable.
    row = await reg.record(
        subphase_id="14.0", claim="evidence register",
        status="complete", test_ref="test_evidence_register_refuses_unevidenced_claims")
    assert row["subphase_id"] == "14.0"
    assert row["test_ref"].startswith("test_evidence_register")


@pytest.mark.asyncio
async def test_evidence_register_upserts_and_summarizes(db):
    from backend.services.evidence_register import EvidenceRegister
    reg = EvidenceRegister(db)
    await reg.record(subphase_id="14.1", claim="scope", status="done",
                     test_ref="t1")
    await reg.record(subphase_id="14.1", claim="scope v2", status="done",
                     test_ref="t2")   # update in place
    rows = await reg.list_all()
    assert len(rows) == 1 and rows[0]["claim"] == "scope v2"
    summary = await reg.summary()
    assert summary["total"] == 1 and summary["blocking"] == []


# ── §14.1: the routing release scope ───────────────────────────────────

def test_contextual_selector_is_release_scoped():
    """The contextual selector engages ONLY when the 'routing' stage is
    enabled — the default-safe degradation is the baseline ranking."""
    from backend.services.improvement_governance import (
        contextual_routing_enabled)

    class S:  # nothing enabled
        IMPROVEMENT_STAGES_ENABLED = ""
        ROUTING_STAGE_ENABLED = False
    assert contextual_routing_enabled(S) is False

    class S2:  # the stage flag alone
        IMPROVEMENT_STAGES_ENABLED = ""
        ROUTING_STAGE_ENABLED = True
    assert contextual_routing_enabled(S2) is True

    class S3:  # via the shared stage list
        IMPROVEMENT_STAGES_ENABLED = "comparisons,routing"
        ROUTING_STAGE_ENABLED = False
    assert contextual_routing_enabled(S3) is True


# ── §14.2: per-category measured summary ───────────────────────────────

@pytest.mark.asyncio
async def test_summary_reports_quality_cost_latency_by_category(db):
    from backend.models.routing_decision import RoutingDecision
    from backend.repositories.routing_repository import (
        RoutingDecisionRepository, new_decision_id)
    r = RoutingDecisionRepository(db)
    for i, (cls, lat, budget) in enumerate([
            ("verified", 120.0, 5), ("verified", 180.0, 7),
            ("rejected", 90.0, 4), ("missing", None, None)]):
        await r.log_decision(RoutingDecision(
            decision_id=new_decision_id(), run_id=f"run-s-{i}",
            task_id=f"t{i}", task_category="research",
            eligible_actions=["m"], selected_action="m",
            selection_probability=1.0, policy_version="pv",
            resolved_action="m", latency_ms=lat, budget_units=budget))
    await r.attach_feedback("run-s-0", "verified", True)
    await r.attach_feedback("run-s-1", "verified", True)
    await r.attach_feedback("run-s-2", "rejected", False)
    out = await r.summary_by_category()
    cat = out["categories"]["research"]
    assert cat["decisions"] == 4 and cat["scored"] == 3
    assert cat["missing_feedback"] == 1, "missing feedback is explicit"
    # verified quality = (1 + 1 + 0) / 3 = 0.6667
    assert cat["verified_quality"] == 0.6667
    assert cat["ci95_low"] is not None and cat["ci95_high"] is not None
    # cost and latency come from the measured fields only
    assert cat["avg_cost_units"] == round((5 + 7 + 4) / 3, 3)
    assert cat["avg_latency_ms"] == round((120 + 180 + 90) / 3, 1)
    assert cat["confidence"] in ("low", "medium", "high")


# ── §14.9/§14.10: the action catalog + eligibility filter ──────────────

def test_catalog_marks_unavailable_actions_with_reasons():
    from backend.services.routing_catalog import catalog_actions
    actions = catalog_actions(
        allowed_models=["gemini-3.5-flash", "gemini-3.6-flash"],
        primary_model="gemini-4.0-flash",
        flash_models=["gemini-3.7-flash", "gemini-3.6-flash",
                      "gemini-3.5-flash", "gemini-3.5-flash-lite"])
    by_name = {a.name: a for a in actions}
    assert by_name["gemini-3.6-flash"].available is True
    assert by_name["gemini-3.5-flash-lite"].available is False
    assert "allow-list" in by_name["gemini-3.5-flash-lite"].reason_unavailable


def test_eligibility_refuses_pinned_but_unavailable_never_substitutes():
    """§14.10: a user-pinned model that the server has not enabled is a
    REFUSAL — the user's choice never silently runs elsewhere."""
    from backend.services.routing_catalog import (
        RoutingCatalogError, catalog_actions, eligible_actions)
    actions = catalog_actions(
        allowed_models=["m-1"], primary_model="m-1", flash_models=["m-1", "m-2"])
    assert eligible_actions(actions, preferred="m-1") == ["m-1"]
    with pytest.raises(RoutingCatalogError):
        eligible_actions(actions, preferred="m-2")


def test_candidate_models_derive_from_the_catalog():
    """The gemini client's candidate list IS the catalog's eligible set —
    one source, one filter (§14.9)."""
    from backend.services import gemini_client
    from backend.services.routing_catalog import catalog_actions, eligible_actions
    cats = gemini_client.candidate_models()
    actions = catalog_actions(
        allowed_models=gemini_client.allowed_models(),
        primary_model=gemini_client.settings.GEMINI_MODEL,
        flash_models=list(gemini_client.FLASH_MODELS))
    assert cats == eligible_actions(actions)


# ── §14.11: the context schema ─────────────────────────────────────────

def test_context_schema_rejects_unknown_category_and_bad_numbers():
    from backend.models.routing_decision import (
        RoutingContextError, validate_context)
    assert validate_context(task_category="research")["task_category"] == \
        "research"
    with pytest.raises(RoutingContextError, match="Unknown task category"):
        validate_context(task_category="sql_injection")
    with pytest.raises(RoutingContextError, match="negative"):
        validate_context(input_size_bytes=-5)
    with pytest.raises(RoutingContextError, match="positive"):
        validate_context(latency_target_ms=0)
    with pytest.raises(RoutingContextError, match="\[0, 1\]"):
        validate_context(selection_probability=1.5)


@pytest.mark.asyncio
async def test_persisted_decisions_pass_the_context_schema(db):
    from backend.models.routing_decision import (
        RoutingContextError, RoutingDecision)
    from backend.repositories.routing_repository import (
        RoutingDecisionRepository, new_decision_id)
    r = RoutingDecisionRepository(db)
    with pytest.raises(RoutingContextError):
        await r.log_decision(RoutingDecision(
            decision_id=new_decision_id(), run_id="run-bad", task_id="t",
            task_category="made_up", eligible_actions=["m"],
            selected_action="m", policy_version="pv"))
    # A valid decision persists.
    await r.log_decision(RoutingDecision(
        decision_id=new_decision_id(), run_id="run-ok", task_id="t",
        task_category="workflow", eligible_actions=["m"],
        selected_action="m", selection_probability=0.9, policy_version="pv"))
    assert len(await r.decisions_for_run("run-ok")) == 1


# ── §14.12: the decision record carries every required field ───────────

@pytest.mark.asyncio
async def test_decision_record_persists_every_contract_field(db):
    from backend.models.routing_decision import RoutingDecision
    from backend.repositories.routing_repository import (
        RoutingDecisionRepository, new_decision_id)
    r = RoutingDecisionRepository(db)
    d = RoutingDecision(
        decision_id=new_decision_id(), run_id="run-full", task_id="t1",
        task_category="browser", capability="core.browser",
        input_size_bytes=1234, latency_target_ms=2000, budget_units=9,
        eligible_actions=["m-a", "m-b"], selected_action="m-a",
        selection_mechanism="contextual_ucb1_explore",
        selection_probability=0.05, latency_ms=321.0,
        policy_version="ucb1-v1-verified-feedback",
        resolved_action="m-b", fallback_from="m-a")
    await r.log_decision(d)
    await r.attach_feedback("run-full", "verified", True,
                            evaluation_id="ev-1")
    row = (await r.decisions_for_run("run-full"))[0]
    # The §14.4 record contract: context, eligible set, choice,
    # probability, mechanism, policy version, feedback columns.
    assert row["task_category"] == "browser"
    assert row["capability"] == "core.browser"
    assert row["input_size_bytes"] == 1234
    assert row["latency_target_ms"] == 2000
    assert row["budget_units"] == 9
    assert row["eligible_actions"] == ["m-a", "m-b"]
    assert row["selected_action"] == "m-a"
    assert row["selection_mechanism"] == "contextual_ucb1_explore"
    assert row["selection_probability"] == 0.05
    assert row["policy_version"] == "ucb1-v1-verified-feedback"
    assert row["resolved_action"] == "m-b"
    assert row["fallback_from"] == "m-a"
    assert row["reward_class"] == "verified"
    assert row["evaluation_id"] == "ev-1"
    assert row["feedback_at"] is not None


# ── §14.13: ONE selector — no parallel selection paths ─────────────────

def test_the_engine_has_a_single_selection_site():
    """AST guard: exactly ONE call site selects models for tasks — the
    engine's contextual/`adaptive.rank` path. A second selector would
    fork the decision log's meaning (whose probabilities? whose
    context?)."""
    import ast as _ast
    engine = (BACKEND_ROOT / "engine/engine.py").read_text()
    tree = _ast.parse(engine)
    selects = [n for n in _ast.walk(tree)
               if isinstance(n, _ast.Call)
               and isinstance(n.func, _ast.Attribute)
               and n.func.attr == "select"
               and isinstance(n.func.value, _ast.Name)
               and "contextual" in n.func.value.id.lower()]
    ranks = [n for n in _ast.walk(tree)
             if isinstance(n, _ast.Call)
             and isinstance(n.func, _ast.Attribute)
             and n.func.attr == "rank"]
    assert len(selects) == 1, "one contextual selection site"
    assert len(ranks) == 1, "one global ranking site (the baseline)"


import pathlib
BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
