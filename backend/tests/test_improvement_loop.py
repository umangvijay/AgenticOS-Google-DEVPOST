"""Phase 13 acceptance tests: the controlled self-improvement loop.

- Baseline and candidate evaluated on the SAME held-out set; grading
  rules pinned by hash cannot be swapped mid-flight.
- Shadow → bounded canary; the cohort cap is predeclared and enforced.
- A candidate that improves AVERAGE quality but violates a critical
  constraint cannot promote.
- Separated permissions: agents cannot evaluate/approve/promote/roll
  back; the proposer cannot approve their own candidate.
- Rollback restores the prior ref and records evidence.
- Every promotion links dataset, results, actor, and deployment version.
"""
import json

import pytest
import pytest_asyncio

from backend.models.experiment import (
    ExperimentKind,
    ExperimentState,
    ExperimentThresholds,
)
from backend.repositories.experiment_repository import (
    ExperimentRepository, set_experiment_repo,
)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.improvement_service import (
    ImprovementError, ImprovementService, dataset_hash_for, grading_hash_for,
)

DATASET = {"tasks": [{"id": "t1", "input": "a"}, {"id": "t2", "input": "b"}]}


def _thresholds(**overrides) -> ExperimentThresholds:
    defaults = dict(success_min=0.8, regression_max=0.0,
                    regression_metrics=("success",), cost_max_usd=1.0,
                    max_cohort_fraction=0.1, max_cohort_size=50,
                    critical_constraints=())
    defaults.update(overrides)
    return ExperimentThresholds(**defaults)


def _proposer_kwargs(**overrides):
    defaults = dict(
        kind=ExperimentKind.PROMPT, target="prompt:read-only-summary",
        hypothesis="Shorter system prompt reduces drift",
        expected_benefit="+5% verified success on read-only tasks",
        baseline_ref="prompt:v3", candidate_ref="prompt:v4",
        dataset_ref="heldout-read-only-2026-11", dataset=DATASET,
        thresholds=_thresholds(), proposer="operator:alice")
    defaults.update(overrides)
    return defaults


@pytest_asyncio.fixture
async def service(tmp_path):
    db = DatabaseManager(str(tmp_path / "experiments.db"))
    await db.initialize()
    repo = ExperimentRepository(db)
    set_experiment_repo(repo)
    svc = ImprovementService(repo)
    try:
        yield svc
    finally:
        set_experiment_repo(None)
        await db.close()


async def _evaluated(service, proposer="operator:alice",
                     evaluator="operator:bob", baseline=None,
                     candidate=None, thresholds=None) -> str:
    """Propose + evaluate with scripted metrics, returning the id."""
    exp = await service.propose(
        **_proposer_kwargs(thresholds=thresholds or _thresholds(),
                           proposer=proposer))
    cand = candidate or {"success": 0.90, "cost_usd": 0.5}
    # §13.12: typed gates require a sample count + the server-owned
    # mandatory constraints on every promotion path.
    cand.setdefault("samples", 25)
    cand.setdefault("constraint_no_unauthorized_actions", True)
    cand.setdefault("constraint_no_unresolved_required_effects", True)
    cand.setdefault("constraint_no_unverified_success", True)
    metrics = {
        "baseline": baseline or {"success": 0.80, "cost_usd": 0.5},
        "candidate": cand,
    }

    def run_evaluator(variant, dataset):
        return metrics["baseline" if variant.endswith("v3") else "candidate"]

    await service.evaluate(exp.experiment_id, evaluator=evaluator,
                           run_evaluator=run_evaluator, dataset=DATASET)
    await service.start_shadow(exp.experiment_id, activator="operator:carol")
    await service.start_canary(exp.experiment_id, activator="operator:carol",
                               cohort_size=10)
    return exp.experiment_id


# ── §13.2: same held-out set, immutable grading ────────────────────────

@pytest.mark.asyncio
async def test_evaluation_refuses_a_swapped_dataset(service):
    exp = await service.propose(**_proposer_kwargs())
    def run_evaluator(variant, dataset):
        return {"success": 1.0}
    with pytest.raises(ImprovementError, match="hash pinned at proposal"):
        await service.evaluate(exp.experiment_id, evaluator="operator:bob",
                               run_evaluator=run_evaluator,
                               dataset={"tasks": [{"id": "TAMPERED"}]})


@pytest.mark.asyncio
async def test_grading_hash_binds_thresholds(service):
    exp = await service.propose(**_proposer_kwargs())
    assert exp.grading_hash == grading_hash_for(_thresholds())
    tampered = _thresholds(success_min=0.1)
    assert exp.grading_hash != grading_hash_for(tampered)


@pytest.mark.asyncio
async def test_baseline_and_candidate_share_one_dataset(service):
    exp = await service.propose(**_proposer_kwargs())
    seen_datasets = []

    def run_evaluator(variant, dataset):
        seen_datasets.append(dataset_hash_for(dataset))
        return {"success": 0.9}

    await service.evaluate(exp.experiment_id, evaluator="operator:bob",
                           run_evaluator=run_evaluator, dataset=DATASET)
    fetched = await service.repo.get_experiment(exp.experiment_id)
    results = json.loads(fetched.results_json)
    assert len(seen_datasets) == 2
    assert seen_datasets[0] == seen_datasets[1] == results["dataset_hash"]


# ── §13.3: shadow → bounded canary ─────────────────────────────────────

@pytest.mark.asyncio
async def test_canary_cohort_cap_is_enforced(service):
    exp = await service.propose(**_proposer_kwargs())
    await service.evaluate(exp.experiment_id, evaluator="operator:bob",
                           run_evaluator=lambda v, d: {"success": 0.9},
                           dataset=DATASET)
    await service.start_shadow(exp.experiment_id, activator="operator:carol")
    with pytest.raises(ImprovementError, match="predeclared"):
        await service.start_canary(exp.experiment_id,
                                   activator="operator:carol", cohort_size=51)


@pytest.mark.asyncio
async def test_canary_fraction_check(service):
    exp_id = await _evaluated(service)
    assert await service.check_cohort_fraction(exp_id, total_population=200) is True
    assert await service.check_cohort_fraction(exp_id, total_population=50) is False


# ── §13 acceptance: constraint veto beats average improvement ──────────

@pytest.mark.asyncio
async def test_critical_constraint_violation_blocks_promotion(service):
    """THE acceptance gate: candidate improves average success 0.80 → 0.96
    but violates 'no_pii_in_prompt' — promotion is refused."""
    exp_id = await _evaluated(
        service,
        candidate={"success": 0.96, "cost_usd": 0.4,
                   "constraint_no_pii_in_prompt": False},
        thresholds=_thresholds(critical_constraints=("no_pii_in_prompt",)))
    with pytest.raises(ImprovementError, match="Critical constraint"):
        await service.promote(exp_id, approver="operator:dave",
                              deployment_version="v1", total_population=200)


@pytest.mark.asyncio
async def test_threshold_failures_block_promotion(service):
    # Below the success floor.
    exp_id = await _evaluated(service, candidate={"success": 0.5,
                                                  "cost_usd": 0.5})
    with pytest.raises(ImprovementError, match="floor"):
        await service.promote(exp_id, approver="operator:dave",
                              deployment_version="v1", total_population=200)

    # Regresses vs baseline (above the floor, below the baseline).
    exp_id = await _evaluated(service, baseline={"success": 0.95,
                                                 "cost_usd": 0.5},
                              candidate={"success": 0.85, "cost_usd": 0.5})
    with pytest.raises(ImprovementError, match="regressed"):
        await service.promote(exp_id, approver="operator:dave",
                              deployment_version="v1", total_population=200)


# ── §13.4: separated permissions ───────────────────────────────────────

@pytest.mark.asyncio
async def test_agents_cannot_promote_or_rollback(service):
    exp_id = await _evaluated(service)
    with pytest.raises(ImprovementError, match="Agents cannot"):
        await service.promote(exp_id, approver="agent:orchestrator",
                              deployment_version="v1", total_population=200)


@pytest.mark.asyncio
async def test_proposer_cannot_approve_own_candidate(service):
    exp_id = await _evaluated(service, proposer="operator:alice")
    with pytest.raises(ImprovementError, match="own candidate"):
        await service.promote(exp_id, approver="operator:alice",
                              deployment_version="v1", total_population=200)


@pytest.mark.asyncio
async def test_agent_cannot_propose_authorization_changes(service):
    with pytest.raises(ImprovementError, match="protected target"):
        await service.propose(**_proposer_kwargs(
            target="authorization:admission-policy",
            proposer="agent:improver"))


@pytest.mark.asyncio
async def test_agent_cannot_grade(service):
    exp = await service.propose(**_proposer_kwargs())
    with pytest.raises(ImprovementError, match="Agents cannot control grading"):
        await service.evaluate(exp.experiment_id, evaluator="agent:improver",
                               run_evaluator=lambda v, d: {"success": 1.0},
                               dataset=DATASET)


@pytest.mark.asyncio
async def test_state_machine_requires_canary_before_promotion(service):
    exp = await service.propose(**_proposer_kwargs())
    await service.evaluate(exp.experiment_id, evaluator="operator:bob",
                           run_evaluator=lambda v, d: {"success": 0.9},
                           dataset=DATASET)
    with pytest.raises(ImprovementError, match="from CANARY"):
        await service.promote(exp.experiment_id, approver="operator:dave",
                              deployment_version="v1")


# ── §13 acceptance: promotion evidence + rollback ──────────────────────

@pytest.mark.asyncio
async def test_promotion_links_dataset_results_actor_version(service):
    exp_id = await _evaluated(
        service,
        candidate={"success": 0.9, "cost_usd": 0.5,
                   "constraint_no_pii": True},
        thresholds=_thresholds(critical_constraints=("no_pii",)))
    promoted = await service.promote(exp_id, approver="operator:dave",
                                     deployment_version="deploy-2026.11.2",
                                     total_population=200)
    assert promoted.state is ExperimentState.PROMOTED
    assert promoted.approved_by == "operator:dave"
    assert promoted.promoted_dataset_hash == promoted.dataset_hash
    results = json.loads(promoted.promoted_results_json)
    assert results["candidate"]["success"] == 0.9
    assert promoted.promoted_actor == "operator:dave"
    assert promoted.deployment_version == "deploy-2026.11.2"


@pytest.mark.asyncio
async def test_rollback_restores_prior_ref_and_records_evidence(service):
    restored = {}

    async def restore(exp):
        restored["ref"] = exp.baseline_ref
        restored["target"] = exp.target

    exp_id = await _evaluated(
        service, candidate={"success": 0.9, "cost_usd": 0.5,
                            "constraint_x": True},
        thresholds=_thresholds(critical_constraints=("x",)))
    await service.promote(exp_id, approver="operator:dave",
                          deployment_version="v1", total_population=200)
    rolled = await service.rollback(exp_id, operator="operator:erin",
                                    reason="regression spike in canary follow-up",
                                    restore=restore)
    assert rolled.state is ExperimentState.ROLLED_BACK
    assert rolled.rolled_back_by == "operator:erin"
    assert "regression spike" in rolled.rollback_reason
    assert restored == {"ref": "prompt:v3", "target": "prompt:read-only-summary"}


@pytest.mark.asyncio
async def test_rollback_requires_promoted_state(service):
    exp = await service.propose(**_proposer_kwargs())
    with pytest.raises(ImprovementError, match="PROMOTED"):
        await service.rollback(exp.experiment_id, operator="operator:erin",
                               reason="early")


# ── durability: records survive restart ────────────────────────────────

@pytest.mark.asyncio
async def test_experiment_records_survive_restart(tmp_path):
    db_path = str(tmp_path / "persist.db")
    db = DatabaseManager(db_path)
    await db.initialize()
    repo = ExperimentRepository(db)
    set_experiment_repo(repo)
    svc = ImprovementService(repo)
    exp = await svc.propose(**_proposer_kwargs())
    await db.close()

    db2 = DatabaseManager(db_path)
    await db2.initialize()
    fresh = ExperimentRepository(db2)
    fetched = await fresh.get_experiment(exp.experiment_id)
    assert fetched is not None
    assert fetched.hypothesis == exp.hypothesis
    assert fetched.dataset_hash == exp.dataset_hash
    await db2.close()


# ── §13.1: failure clusters feed proposals ─────────────────────────────

def test_failure_cluster_summary_groups_by_category():
    from backend.services.improvement_service import failure_cluster_summary
    episodes = [
        {"episode_id": "ep1", "failure_category": "timeout"},
        {"episode_id": "ep2", "failure_category": "timeout"},
        {"episode_id": "ep3", "failure_category": "schema"},
    ]
    summary = failure_cluster_summary(episodes)
    assert summary["clusters"][0]["category"] == "timeout"
    assert summary["clusters"][0]["count"] == 2
    assert summary["clusters"][1]["category"] == "schema"


# ── §13.12: typed evidence gates (review finding I) ────────────────────

def _typed_thresholds(**over):
    from backend.models.experiment import ExperimentThresholds
    base = dict(success_min=0.8, regression_max=0.0,
                regression_metrics=("success",), max_cohort_fraction=0.1,
                max_cohort_size=50, critical_constraints=())
    base.update(over)
    return ExperimentThresholds(**base)


async def _promote_with(service, candidate, thresholds=None):
    exp = await service.propose(**_proposer_kwargs(thresholds=thresholds or _typed_thresholds()))
    def run_evaluator(variant, dataset):
        return {"baseline": {"success": 0.85, "cost_usd": 0.5,
                             "latency_ms": 2000.0},
                "candidate": candidate}[
            "baseline" if variant.endswith("v3") else "candidate"]
    await service.evaluate(exp.experiment_id, evaluator="operator:bob",
                           run_evaluator=run_evaluator, dataset=DATASET)
    await service.start_shadow(exp.experiment_id, activator="operator:c")
    await service.start_canary(exp.experiment_id, activator="operator:c",
                               cohort_size=5)
    return await service.promote(exp.experiment_id, approver="operator:d",
                                 deployment_version="v1", total_population=200)


@pytest.mark.asyncio
async def test_missing_metrics_cannot_default_to_zero(service):
    """§13.12 acceptance: missing measurements stay missing — promotion
    refuses rather than treating them as 0 (perfect on cost-style gates)."""
    with pytest.raises(ImprovementError, match="not a finite number"):
        await _promote_with(service, {"success": 0.9, "cost_usd": None,
                                      "samples": 25},
                            thresholds=_typed_thresholds(cost_max_usd=1.0))


@pytest.mark.asyncio
async def test_nan_and_boolean_metrics_refused(service):
    with pytest.raises(ImprovementError, match="not a finite number"):
        await _promote_with(service, {"success": float("nan"), "samples": 25})
    with pytest.raises(ImprovementError, match="not a finite number"):
        await _promote_with(service, {"success": True, "samples": 25})


@pytest.mark.asyncio
async def test_unevaluated_evidence_cannot_promote(service):
    with pytest.raises(ImprovementError, match="no sample count"):
        await _promote_with(service, {"success": 0.95})


@pytest.mark.asyncio
async def test_server_owned_constraints_cannot_be_omitted(service):
    """§13.12: unauthorized actions / unresolved effects / unverified
    success are SERVER-OWNED — a proposal that omits them is still
    gated."""
    with pytest.raises(ImprovementError, match="Critical constraint"):
        await _promote_with(service, {
            "success": 0.95, "samples": 25,
            # constraint_no_unauthorized_actions deliberately absent
        })


@pytest.mark.asyncio
async def test_latency_regression_is_direction_aware(service):
    """§13.12: latency is LOWER-is-better — a candidate with higher
    latency regresses even though 'success' improved."""
    with pytest.raises(ImprovementError, match="regressed on 'latency_ms'"):
        await _promote_with(service, {
            "success": 0.9, "latency_ms": 5000.0, "samples": 25,
        }, thresholds=_typed_thresholds(
            regression_metrics=("success", "latency_ms"),
            regression_metrics_lower_is_better=("latency_ms",)))
