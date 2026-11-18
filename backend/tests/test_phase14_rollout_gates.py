"""Phase 14 §14.26–§14.29: shadow execution, bounded canary, the
deployment adapter, and drift windows — each as its own named row."""
import json

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "rollout14.db"))
    await db.initialize()
    from backend.repositories.experiment_repository import (
        ExperimentRepository, set_experiment_repo)
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository)
    repo = ExperimentRepository(db)
    set_experiment_repo(repo)
    try:
        yield {"db": db, "repo": repo,
               "stats": SQLiteAdaptivePolicyRepository(db)}
    finally:
        set_experiment_repo(None)
        await db.close()


async def _experiment(env, state="SHADOW"):
    """A REAL experiment through the service (the API's exact path)."""
    from backend.services.improvement_service import ImprovementService
    from backend.models.experiment import ExperimentKind, ExperimentThresholds
    service = ImprovementService(env["repo"])
    exp = await service.propose(
        kind=ExperimentKind.PROMPT, target="prompt:p-14.26",
        hypothesis="candidate replies better",
        expected_benefit="+10% verified quality",
        baseline_ref="v1", candidate_ref="v2",
        dataset_ref="ds", dataset={"cases": []},
        thresholds=ExperimentThresholds(success_min=0.9),
        proposer="operator:alice")
    if state in ("SHADOW", "CANARY"):
        # The legal path to shadow runs through evaluation (§13 states):
        # PROPOSED → EVALUATING → EVALUATED → SHADOW.
        exp = await env["repo"].transition(
            exp.experiment_id, __import__(
                "backend.models.experiment",
                fromlist=["ExperimentState"]).ExperimentState.EVALUATING,
            role="evaluator", actor="evaluator:eve")
        exp = await env["repo"].transition(
            exp.experiment_id, __import__(
                "backend.models.experiment",
                fromlist=["ExperimentState"]).ExperimentState.EVALUATED,
            role="evaluator", actor="evaluator:eve",
            results={"baseline": {"success": 0.8},
                     "candidate": {"success": 0.92}})
        exp = await service.start_shadow(exp.experiment_id,
                                         activator="operator:alice")
        if state == "CANARY":
            exp = await service.start_canary(exp.experiment_id,
                                             activator="operator:alice",
                                             cohort_size=10)
    return exp


# ── §14.26: shadow execution measures without serving ──────────────────

@pytest.mark.asyncio
async def test_shadow_records_measured_observations_not_state_labels(env):
    """§13.16 through §14.26: a shadow comparison executes the candidate
    and records MEASURED observations (outcome + latency + cost) —
    'shadow observed' requires recorded execution, never a label."""
    from backend.services.experiment_comparison import (
        ExperimentComparisonRepository, set_comparison_repo,
        drain_comparisons)
    exp = await _experiment(env, state="SHADOW")
    comp_repo = ExperimentComparisonRepository(env["db"])
    set_comparison_repo(comp_repo)
    try:
        comp = await comp_repo.enqueue(
            exp.experiment_id, cases=[{"case_id": "c1", "input": "x"}],
            baseline_ref=exp.baseline_ref, candidate_ref=exp.candidate_ref,
            revision_hash=json.dumps({"rev": "shadow"}, sort_keys=True))
        observed = []

        async def run_case(case, variant_ref):
            observed.append(variant_ref)
            return {"verdict": "pass",
                    "detail": {"latency_ms": 42.0, "token_units": 15}}

        await drain_comparisons("w-shadow", run_case=run_case,
                                experiment_repo=env["repo"])
        assert set(observed) == {exp.baseline_ref, exp.candidate_ref}, \
            "the candidate EXECUTED (measured), in shadow"
        summary = await env["repo"].shadow_summary(exp.experiment_id)
        assert summary["observations"] >= 1, \
            "candidate executions are DURABLY recorded as observations"
        assert summary["outcomes"], "the outcome distribution is measured"
        assert summary["mean_latency_ms"] is not None, \
            "an observation is a measurement (latency), not a state label"
    finally:
        set_comparison_repo(None)


# ── §14.27: the canary cohort is bounded by the predeclared fraction ────

@pytest.mark.asyncio
async def test_canary_cohort_fraction_is_enforced(env):
    """The running canary stays inside the predeclared fraction of the
    population (20 of 100 here) — over it refuses; within it allows."""
    from backend.services.improvement_service import ImprovementError, ImprovementService
    exp = await _experiment(env, state="CANARY")
    service = ImprovementService(env["repo"])
    # The cohort admits up to its cap (10), then refuses (absolute cap).
    for i in range(10):
        assert await env["repo"].record_exposure(
            exp.experiment_id, subject_id=f"user-{i}",
            variant="candidate",
            max_cohort_size=exp.cohort_size) is True
    assert await env["repo"].record_exposure(
        exp.experiment_id, subject_id="user-over",
        variant="candidate",
        max_cohort_size=exp.cohort_size) is False, \
        "the absolute cap is hard"
    # 10 of 100 = exactly the predeclared 0.1 fraction → inside.
    assert await service.check_cohort_fraction(exp.experiment_id,
                                               total_population=100) is True
    # The same cohort against a smaller population exceeds the fraction.
    assert await service.check_cohort_fraction(exp.experiment_id,
                                               total_population=90) is False


# ── §14.28: the deployment adapter resolves variants deterministically ──

def test_variant_assignment_is_deterministic_and_bounded():
    from backend.services.improvement_rollout import assign_variant
    seen = {}
    for i in range(2000):
        v = assign_variant("p-x", f"subject-{i}", "exp-x", 0.25)
        seen[v] = seen.get(v, 0) + 1
    # Deterministic: the same triple always resolves the same way.
    assert assign_variant("p-x", "subject-7", "exp-x", 0.25) == \
        assign_variant("p-x", "subject-7", "exp-x", 0.25)
    # The fraction is honored within sampling noise (25% ± a few points).
    share = seen.get("candidate", 0) / 2000
    assert 0.20 <= share <= 0.30, f"canary share {share} broke the bound"
    # Degenerate fractions.
    assert assign_variant("p", "s", "e", 1.0) == "candidate"
    assert assign_variant("p", "s", "e", 0.0) == "baseline"


@pytest.mark.asyncio
async def test_instruction_resolution_names_its_source_and_version(env):
    """§13.14 through §14.28: the resolved instructions carry the
    template, the version identity, and the SOURCE — provenance is
    persisted with the attempt, never lost."""
    from backend.services.improvement_rollout import resolve_task_instructions
    # Without a store: an explicit input is honest about its origin.
    out = await resolve_task_instructions(
        {"_instructions": "v1 rules", "_task_category": "workflow"}, "u1")
    assert out == {"template": "v1 rules", "version_id": None,
                   "source": "task_input"}
    # Nothing to resolve → None (never a fabricated template).
    assert await resolve_task_instructions({"goal": "x"}, "u1") is None


# ── §14.29: drift windows measure the RECENT window, not everything ─────

@pytest.mark.asyncio
async def test_drift_windows_separate_recent_from_long_run(env):
    from backend.services.contextual_policy import ContextualRoutingPolicy
    policy = ContextualRoutingPolicy(env["stats"])
    ctx_key = __import__("backend.services.contextual_policy",
                         fromlist=["context_bucket"]).context_bucket(
                             "research", "core.search", 10)
    # Long-run: 30 verified outcomes (mean 1.0), window started now.
    for _ in range(30):
        await policy.learn(model="m-a", task_category="research",
                           capability="core.search", input_size_bytes=10,
                           reward_class="verified")
    report = await policy.drift_report()
    row = next(r for r in report["contexts"]
               if r["context_key"] == ctx_key)
    assert row["recent_mean_reward"] == 1.0
    assert row["long_run_mean_reward"] == 1.0
    assert row["drift_detected"] is False
    # The recent window flips to failures → recent ≠ long-run.
    for _ in range(25):
        await policy.learn(model="m-a", task_category="research",
                           capability="core.search", input_size_bytes=10,
                           reward_class="rejected")
    report = await policy.drift_report()
    row = next(r for r in report["contexts"]
               if r["context_key"] == ctx_key)
    assert row["recent_mean_reward"] < row["long_run_mean_reward"], \
        "the recent window reflects RECENT outcomes, not the long run"
