"""Phase 13 chat-7 acceptance tests (§13.10/§13.11/§13.18)."""
import json

import pytest
import pytest_asyncio

from backend.models.experiment import (
    ExperimentKind, ExperimentState, ExperimentThresholds)
from backend.repositories.experiment_repository import (
    ExperimentRepository, set_experiment_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.evaluation_datasets import (
    DatasetError, EvaluationDatasetService, REQUIRED_CASE_FAMILIES,
)
from backend.services.improvement_service import ImprovementError, ImprovementService
from backend.services.prompt_versions import (
    PromptVersionStore, set_prompt_version_store)

BASELINE = "BASE {goal}"
CANDIDATE = "CAND {goal}"


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "chat7.db"))
    await db.initialize()
    store = PromptVersionStore(db)
    repo = ExperimentRepository(db)
    datasets = EvaluationDatasetService(db)
    set_experiment_repo(repo)
    set_prompt_version_store(store)
    try:
        yield {"db": db, "store": store, "repo": repo, "datasets": datasets,
               "service": ImprovementService(repo, prompt_store=store)}
    finally:
        set_experiment_repo(None)
        set_prompt_version_store(None)
        await db.close()


# ── §13.10: the immutable experiment manifest ──────────────────────────

@pytest.mark.asyncio
async def test_manifest_pins_real_versions_and_content_hashes(env):
    """§13.10: propose resolves and pins the ACTUAL prompt versions with
    content hashes; a reviewer can reconstruct what was compared."""
    store = env["store"]
    b = await store.checkout("p-m", BASELINE, created_by="op")
    await store.activate("p-m", b["version_id"], activated_by="op")
    c = await store.checkout("p-m", CANDIDATE, created_by="agent:x")
    exp = await env["service"].propose(
        kind=ExperimentKind.PROMPT, target="prompt:p-m",
        hypothesis="h", expected_benefit="b",
        baseline_ref=b["version_id"], candidate_ref=c["version_id"],
        dataset_ref="ds", dataset={"cases": [1]},
        thresholds=ExperimentThresholds(success_min=0.8),
        proposer="operator:a")
    manifest = json.loads(exp.manifest_json)
    assert manifest["prompt_versions"]["prompt_id"] == "p-m"
    assert manifest["prompt_versions"]["baseline"]["version_id"] == b["version_id"]
    assert manifest["prompt_versions"]["candidate"]["content_sha256"]
    assert manifest["evaluator_version"] and manifest["rubric_version"]
    assert manifest["metric_protocol"]["mandatory_constraints"]


@pytest.mark.asyncio
async def test_nonexistent_or_unrelated_refs_rejected_at_proposal(env):
    """§13.10: a ref that does not resolve to a version of the TARGET is
    rejected at proposal — never silently carried."""
    with pytest.raises(ImprovementError, match="does not resolve"):
        await env["service"].propose(
            kind=ExperimentKind.PROMPT, target="prompt:p-m",
            hypothesis="h", expected_benefit="b",
            baseline_ref="pv_does_not_exist", candidate_ref="pv_also_no",
            dataset_ref="ds", dataset={"cases": [1]},
            thresholds=ExperimentThresholds(success_min=0.8),
            proposer="operator:a")


@pytest.mark.asyncio
async def test_content_mutation_invalidates_the_experiment(env):
    """§13.10 acceptance: changing a pinned component (prompt content
    under the same version id) requires a NEW experiment — promotion
    refuses."""
    store = env["store"]
    b = await store.checkout("p-mut", BASELINE, created_by="op")
    await store.activate("p-mut", b["version_id"], activated_by="op")
    c = await store.checkout("p-mut", CANDIDATE, created_by="agent:x")
    exp = await env["service"].propose(
        kind=ExperimentKind.PROMPT, target="prompt:p-mut",
        hypothesis="h", expected_benefit="b",
        baseline_ref=b["version_id"], candidate_ref=c["version_id"],
        dataset_ref="ds", dataset={"cases": [1]},
        thresholds=ExperimentThresholds(success_min=0.8),
        proposer="operator:a")
    await env["service"].evaluate(
        exp.experiment_id, evaluator="operator:b",
        run_evaluator=lambda v, d: {"success": 0.9, "samples": 20,
                                    "constraint_no_unauthorized_actions": True,
                                    "constraint_no_unresolved_required_effects": True,
                                    "constraint_no_unverified_success": True},
        dataset={"cases": [1]})
    await env["service"].start_shadow(exp.experiment_id, activator="operator:c")
    await env["service"].start_canary(exp.experiment_id,
                                      activator="operator:c", cohort_size=5)
    # MUTATE the candidate content under the SAME version id.
    await env["db"].execute(
        "UPDATE prompt_versions SET template='TAMPERED {goal}' WHERE version_id=?",
        (c["version_id"],))
    await env["db"].commit()
    with pytest.raises(ImprovementError, match="NEW experiment is required"):
        await env["service"].promote(exp.experiment_id, approver="operator:d",
                                     deployment_version="v1",
                                     total_population=200)


# ── §13.11: versioned datasets, draft-proof held-out ───────────────────

def _compliant_heldout_cases():
    return [
        {"case_id": "h1", "family": "supported_success",
         "input": "summarize the report", "expected": "verified summary"},
        {"case_id": "h2", "family": "unsupported_task",
         "input": "book a flight", "expected": "explicit unsupported"},
        {"case_id": "h3", "family": "missing_credentials",
         "input": "read calendar", "expected": "credential request"},
        {"case_id": "h4", "family": "ambiguous_outcome",
         "input": "send the email", "expected": "reconciliation"},
        {"case_id": "h5", "family": "malformed_output",
         "input": "parse the response", "expected": "needs_review"},
        {"case_id": "h6", "family": "factuality_failure",
         "input": "resume draft", "expected": "flagged unsupported fact"},
    ]


@pytest.mark.asyncio
async def test_heldout_set_requires_all_case_families(env):
    with pytest.raises(DatasetError, match="missing:"):
        await env["datasets"].create_dataset(
            name="heldout", set_kind="heldout",
            cases=[{"case_id": "h1", "family": "supported_success"}],
            created_by="operator:a")
    created = await env["datasets"].create_dataset(
        name="heldout", set_kind="heldout",
        cases=_compliant_heldout_cases(), created_by="operator:a",
        provenance="reviewed fixtures 2026-11")
    assert created["version"] == 1
    assert len(created["cases"]) == len(REQUIRED_CASE_FAMILIES)


@pytest.mark.asyncio
async def test_drafting_agent_cannot_edit_heldout_expectations(env):
    """§13.11 acceptance: the drafting agent (propose-only principal) can
    NEVER modify the held-out set — only an operator can."""
    created = await env["datasets"].create_dataset(
        name="heldout-dp", set_kind="heldout",
        cases=_compliant_heldout_cases(), created_by="operator:a")
    tampered = _compliant_heldout_cases()
    tampered[0]["expected"] = "anything the candidate wants"
    with pytest.raises(DatasetError, match="draft-proof"):
        await env["datasets"].update_cases(
            created["dataset_id"], cases=tampered, actor="agent:improver")
    # An operator CAN create the next version (history preserved).
    updated = await env["datasets"].update_cases(
        created["dataset_id"], cases=tampered, actor="operator:a")
    assert updated["version"] == 2
    old = await env["datasets"].get_dataset(created["dataset_id"], version=1)
    assert old["cases"][0]["expected"] == "verified summary"


@pytest.mark.asyncio
async def test_evaluation_uses_heldout_cases_verbatim(env):
    """§13.11: baseline and candidate run on the SAME pinned held-out
    cases — the verifier passes them verbatim, unfiltered."""
    created = await env["datasets"].create_dataset(
        name="heldout-v", set_kind="heldout",
        cases=_compliant_heldout_cases(), created_by="operator:a")
    cases = EvaluationDatasetService.heldout_cases(created)
    b = await env["store"].checkout("p-h", BASELINE, created_by="op")
    await env["store"].activate("p-h", b["version_id"], activated_by="op")
    c = await env["store"].checkout("p-h", CANDIDATE, created_by="agent:x")
    exp = await env["service"].propose(
        kind=ExperimentKind.PROMPT, target="prompt:p-h",
        hypothesis="h", expected_benefit="b",
        baseline_ref=b["version_id"], candidate_ref=c["version_id"],
        dataset_ref=f"dataset:{created['dataset_id']}@v{created['version']}",
        dataset={"cases": cases},
        thresholds=ExperimentThresholds(success_min=0.8),
        proposer="operator:a")
    seen_datasets = []

    def run_evaluator(variant, dataset):
        seen_datasets.append(dataset["cases"])
        return {"success": 0.9, "samples": len(dataset["cases"])}

    await env["service"].evaluate(exp.experiment_id, evaluator="operator:b",
                                  run_evaluator=run_evaluator, dataset={"cases": cases})
    assert seen_datasets[0] == seen_datasets[1] == cases, \
        "both variants ran on the SAME verbatim held-out cases"


# ── §13.18: evidence before each exposure increase ─────────────────────


@pytest.mark.asyncio
async def test_canary_requires_declared_shadow_evidence(env):
    """§13.18: failed shadow evidence cannot enter a user-serving canary —
    the predeclared minimum of MEASURED observations gates it."""
    store = env["store"]
    b = await store.checkout("p-gate", BASELINE, created_by="op")
    await store.activate("p-gate", b["version_id"], activated_by="op")
    c = await store.checkout("p-gate", CANDIDATE, created_by="agent:x")
    exp = await env["service"].propose(
        kind=ExperimentKind.PROMPT, target="prompt:p-gate",
        hypothesis="h", expected_benefit="b",
        baseline_ref=b["version_id"], candidate_ref=c["version_id"],
        dataset_ref="ds", dataset={"cases": [1]},
        thresholds=ExperimentThresholds(success_min=0.8,
                                        min_shadow_observations=3),
        proposer="operator:a")
    await env["service"].evaluate(
        exp.experiment_id, evaluator="operator:b",
        run_evaluator=lambda v, d: {"success": 0.9, "samples": 20,
                                    "constraint_no_unauthorized_actions": True,
                                    "constraint_no_unresolved_required_effects": True,
                                    "constraint_no_unverified_success": True},
        dataset={"cases": [1]})
    await env["service"].start_shadow(exp.experiment_id, activator="operator:c")
    # Only 2 of the required 3 observations recorded.
    for i in range(2):
        await env["repo"].record_shadow_observation(
            exp.experiment_id, f"s-{i}", candidate_version_id=c["version_id"],
            outcome="pass", latency_ms=100.0, token_units=30)
    with pytest.raises(ImprovementError, match="Shadow evidence insufficient"):
        await env["service"].start_canary(exp.experiment_id,
                                          activator="operator:c", cohort_size=5)
    # The third observation satisfies the declared bar.
    await env["repo"].record_shadow_observation(
        exp.experiment_id, "s-2", candidate_version_id=c["version_id"],
        outcome="pass", latency_ms=100.0, token_units=30)
    moved = await env["service"].start_canary(exp.experiment_id,
                                              activator="operator:c",
                                              cohort_size=5)
    assert moved.state is ExperimentState.CANARY


@pytest.mark.asyncio
async def test_promotion_requires_declared_canary_exposures(env):
    """§13.18: promotion cannot reuse offline/shadow numbers as proof of
    canary performance — counted exposures gate it."""
    store = env["store"]
    b = await store.checkout("p-gate2", BASELINE, created_by="op")
    await store.activate("p-gate2", b["version_id"], activated_by="op")
    c = await store.checkout("p-gate2", CANDIDATE, created_by="agent:x")
    exp = await env["service"].propose(
        kind=ExperimentKind.PROMPT, target="prompt:p-gate2",
        hypothesis="h", expected_benefit="b",
        baseline_ref=b["version_id"], candidate_ref=c["version_id"],
        dataset_ref="ds", dataset={"cases": [1]},
        thresholds=ExperimentThresholds(success_min=0.8,
                                        min_canary_exposures=4),
        proposer="operator:a")
    await env["service"].evaluate(
        exp.experiment_id, evaluator="operator:b",
        run_evaluator=lambda v, d: {"success": 0.9, "samples": 20,
                                    "constraint_no_unauthorized_actions": True,
                                    "constraint_no_unresolved_required_effects": True,
                                    "constraint_no_unverified_success": True},
        dataset={"cases": [1]})
    await env["service"].start_shadow(exp.experiment_id, activator="operator:c")
    await env["service"].start_canary(exp.experiment_id,
                                      activator="operator:c", cohort_size=10)
    # Only 3 of the required 4 exposures admitted.
    for i in range(3):
        await env["repo"].record_exposure(exp.experiment_id, f"s-{i}",
                                          max_cohort_size=10)
    with pytest.raises(ImprovementError, match="Canary evidence insufficient"):
        await env["service"].promote(exp.experiment_id, approver="operator:d",
                                     deployment_version="v1",
                                     total_population=200)
    await env["repo"].record_exposure(exp.experiment_id, "s-3",
                                      max_cohort_size=10)
    promoted = await env["service"].promote(
        exp.experiment_id, approver="operator:d", deployment_version="v1",
        total_population=200)
    assert promoted.state is ExperimentState.PROMOTED


# ── §13.31/§13.33: governance + stage flags ────────────────────────────

@pytest.mark.asyncio
async def test_improvement_calls_meter_through_the_usage_ledger(tmp_path):
    """§13.31: drafting/comparison/shadow calls pass through the SAME
    durable accounting boundary — restarts cannot erase the spend."""
    from backend.models.usage import MeasurementSource
    from backend.repositories.usage_repository import (
        SQLiteUsageRepository, set_usage_repo)
    from backend.services.improvement_governance import meter_improvement_call
    db = DatabaseManager(str(tmp_path / "meter.db"))
    await db.initialize()
    usage = SQLiteUsageRepository(db)
    set_usage_repo(usage)
    try:
        from backend.repositories.usage_repository import BucketLimit
        from backend.models.usage import BudgetScope, ReservationRequest
        # Pre-seed a reservation via the meter itself.
        res_id = await meter_improvement_call(
            usage, user_id="u1", purpose="improvement_drafting", units=250,
            model="gemini-test")
        assert res_id
        # The settled units appear in the ledger's daily figure.
        # The reservation is SETTLED in the durable ledger (the Phase-2
        # accounting boundary — the legacy daily mirror is not the
        # authoritative record).
        settled = await usage.get_reservation(res_id)
        assert settled is not None
        assert settled.state.value == "settled"
        assert settled.units == 250
    finally:
        set_usage_repo(None)
        await db.close()


@pytest.mark.asyncio
async def test_evidence_detail_redaction(env):
    """§13.31: stored evidence keeps references, never raw payloads or
    credentials."""
    from backend.services.improvement_governance import redact_evidence_detail
    out = redact_evidence_detail({
        "case_id": "c1", "raw_response": "SECRET PAGE CONTENT",
        "credentials": {"api_key": "sk-123"},
        "nested": {"token": "x", "verdict": "pass"},
    })
    assert out["raw_response"] == "[REDACTED]"
    assert out["credentials"] == "[REDACTED]"
    assert out["nested"]["token"] == "[REDACTED]"
    assert out["nested"]["verdict"] == "pass"


def test_stage_flags_progressive_rollout():
    """§13.33: evidence collection is always on; each serving stage
    requires its flag and refuses when disabled."""
    from backend.services.improvement_governance import (
        improvement_stage_flags, require_stage, GovernanceError)

    class _Settings:
        IMPROVEMENT_STAGES_ENABLED = "proposals,comparisons"

    flags = improvement_stage_flags(_Settings())
    assert flags["evidence_collection"] is True
    assert flags["proposals"] is True and flags["comparisons"] is True
    assert flags["shadow"] is False and flags["canary"] is False
    assert flags["activation"] is False
    require_stage(flags, "proposals")   # OK
    with pytest.raises(GovernanceError, match="not enabled"):
        require_stage(flags, "activation")

    # Default: everything serving off.
    default_flags = improvement_stage_flags(None)
    assert not any(default_flags[s] for s in
                   ("proposals", "comparisons", "shadow", "canary",
                    "activation"))


@pytest.mark.asyncio
async def test_shadow_observation_retention_purge(env):
    """§13.31: shadow observations older than the retention window purge;
    recent ones stay."""
    store = env["store"]
    b = await store.checkout("p-ret", BASELINE, created_by="op")
    c = await store.checkout("p-ret", CANDIDATE, created_by="agent:x")
    old_ts = 1   # epoch 1: ancient
    await env["repo"].record_shadow_observation(
        "exp-ret", "s-old", candidate_version_id=c["version_id"],
        outcome="pass", latency_ms=10.0)
    await env["db"].execute(
        "UPDATE shadow_observations SET recorded_at=? WHERE experiment_id=?",
        (old_ts, "exp-ret"))
    await env["db"].commit()
    await env["repo"].record_shadow_observation(
        "exp-ret", "s-new", candidate_version_id=c["version_id"],
        outcome="pass", latency_ms=10.0)
    from backend.services.improvement_governance import (
        purge_expired_observations)
    purged = await purge_expired_observations(env["repo"], retention_days=90)
    assert purged >= 1
    assert await env["repo"].shadow_observation_count("exp-ret") == 1
