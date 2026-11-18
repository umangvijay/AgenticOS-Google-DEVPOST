"""Phase 20 §20.22: controlled self-evolution — promote/reject/rollback
decisions through the REAL ImprovementService: deterministic gates hold
(dataset pinning, cohort cap, typed evidence, human-only authority),
rejection is explicit with an actor, and rollback reverts a promoted
candidate through a REAL restore hook."""
import pytest
import pytest_asyncio

from backend.models.experiment import ExperimentKind, ExperimentState, ExperimentThresholds
from backend.repositories.experiment_repository import (
    ExperimentRepository, set_experiment_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.improvement_service import ImprovementService, ImprovementError

pytestmark = pytest.mark.asyncio

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
    db = DatabaseManager(str(tmp_path / "evolution.db"))
    await db.initialize()
    repo = ExperimentRepository(db)
    set_experiment_repo(repo)
    svc = ImprovementService(repo)
    try:
        yield svc
    finally:
        set_experiment_repo(None)
        await db.close()


async def _to_canary(service, *, baseline=None, candidate=None,
                     thresholds=None) -> str:
    exp = await service.propose(
        **_proposer_kwargs(thresholds=thresholds or _thresholds()))
    cand = dict(candidate or {"success": 0.90, "cost_usd": 0.5})
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

    await service.evaluate(exp.experiment_id, evaluator="operator:bob",
                           run_evaluator=run_evaluator, dataset=DATASET)
    await service.start_shadow(exp.experiment_id, activator="operator:carol")
    await service.start_canary(exp.experiment_id, activator="operator:carol",
                               cohort_size=10)
    return exp.experiment_id


# ── PROMOTE ────────────────────────────────────────────────────────────

async def test_promotion_requires_human_non_proposer_approver(service):
    exp_id = await _to_canary(service)
    with pytest.raises(ImprovementError, match="Agents cannot approve"):
        await service.promote(exp_id, approver="agent:executor",
                              deployment_version="6.3.0")
    with pytest.raises(ImprovementError, match="cannot approve their own"):
        await service.promote(exp_id, approver="operator:alice",
                              deployment_version="6.3.0")


async def test_promotion_fails_without_canary_state(service):
    exp = await service.propose(**_proposer_kwargs())
    with pytest.raises(ImprovementError, match="from CANARY"):
        await service.promote(exp.experiment_id, approver="operator:bob",
                              deployment_version="6.3.0")


async def test_promotion_pins_evidence_and_succeeds(service):
    exp_id = await _to_canary(service)
    promoted = await service.promote(exp_id, approver="operator:dave",
                                     deployment_version="6.3.0")
    assert promoted.state is ExperimentState.PROMOTED
    results = promoted.results_json
    assert results and "candidate" in results
    assert promoted.approved_by == "operator:dave"


async def test_promotion_refuses_incomplete_typed_evidence(service):
    exp_id = await _to_canary(
        service, candidate={"success": 0.9, "cost_usd": 0.5, "samples": 25,
                            "constraint_no_unverified_success": False})
    # A violated server-owned constraint vetoes promotion regardless of
    # the average quality (§13.12).
    with pytest.raises(ImprovementError):
        await service.promote(exp_id, approver="operator:dave",
                              deployment_version="6.3.0")


# ── REJECT ─────────────────────────────────────────────────────────────

async def test_rejection_is_explicit_and_recorded(service):
    exp_id = await _to_canary(service)
    rejected = await service.reject(exp_id, approver="operator:dave",
                                    reason="regression risk on cost ceiling")
    assert rejected.state is ExperimentState.REJECTED
    assert rejected.rejection_reason == "regression risk on cost ceiling"
    reloaded = await service.repo.get_experiment(exp_id)
    assert reloaded.rejection_reason == rejected.rejection_reason
    assert reloaded.approved_by == "operator:dave"
    import json
    assert "candidate" in json.loads(reloaded.results_json)
    # A rejected experiment cannot be promoted afterwards.
    with pytest.raises(ImprovementError, match="from CANARY"):
        await service.promote(exp_id, approver="operator:dave",
                              deployment_version="6.3.0")


async def test_agents_cannot_reject_or_rollback(service):
    exp_id = await _to_canary(service)
    with pytest.raises(ImprovementError, match="Agents cannot"):
        await service.reject(exp_id, approver="agent:executor", reason="no")
    with pytest.raises(ImprovementError, match="Agents cannot"):
        await service.rollback(exp_id, operator="agent:executor",
                               reason="no")


# ── ROLLBACK ───────────────────────────────────────────────────────────

async def test_rollback_reverts_promoted_candidate(service):
    restore_calls = {"count": 0}

    async def restore(experiment):
        restore_calls["count"] += 1
        return {"restored": experiment.baseline_ref}

    exp_id = await _to_canary(service)
    promoted = await service.promote(exp_id, approver="operator:dave",
                                     deployment_version="6.3.0")
    rolled = await service.rollback(exp_id, operator="operator:erin",
                                    reason="canary showed error spike",
                                    restore=restore)
    assert rolled.state is ExperimentState.ROLLED_BACK
    assert restore_calls["count"] == 1, \
        "the REAL restore hook ran exactly once (baseline reversion)"
    # Rollback applies ONLY to promoted experiments.
    with pytest.raises(ImprovementError, match="PROMOTED"):
        await service.rollback(exp_id, operator="operator:erin",
                               reason="again", restore=restore)


async def test_rollback_without_restore_hook_still_records_decision(service):
    exp_id = await _to_canary(service)
    await service.promote(exp_id, approver="operator:dave",
                          deployment_version="6.3.0")
    rolled = await service.rollback(exp_id, operator="operator:erin",
                                    reason="manual revert applied out of band")
    assert rolled.state is ExperimentState.ROLLED_BACK
