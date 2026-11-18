"""Phase 13 chat-6 acceptance tests: durable comparisons (§13.13), the
evaluate API (finding E), and measured shadow observations (§13.16)."""
import json

import pytest
import pytest_asyncio

from backend.models.experiment import (
    ExperimentKind, ExperimentState, ExperimentThresholds)
from backend.repositories.experiment_repository import (
    ExperimentRepository, set_experiment_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.experiment_comparison import (
    ExperimentComparisonRepository, drain_comparisons, set_comparison_repo)
from backend.services.improvement_service import ImprovementService

DATASET_CASES = [
    {"case_id": "c1", "input": "invoice query"},
    {"case_id": "c2", "input": "refund query"},
    {"case_id": "c3", "input": "greeting"},
]


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "chat6.db"))
    await db.initialize()
    repo = ExperimentRepository(db)
    comparison = ExperimentComparisonRepository(db)
    set_experiment_repo(repo)
    set_comparison_repo(comparison)
    try:
        yield {"db": db, "repo": repo, "comparisons": comparison,
               "service": ImprovementService(repo)}
    finally:
        set_experiment_repo(None)
        set_comparison_repo(None)
        await db.close()


def _executor(pass_candidate=True, pass_rate_baseline=1.0):
    """Deterministic case executor: the candidate passes unless
    pass_candidate is False; the baseline passes per the rate."""
    calls = {"n": 0}

    async def run_case(case, variant_ref):
        calls["n"] += 1
        if variant_ref.startswith("cand"):
            verdict = "pass" if pass_candidate else "fail"
        else:
            verdict = "pass" if (calls["n"] % max(
                1, round(1 / max(pass_rate_baseline, 0.01))) == 0) else "fail"
        return {"verdict": verdict,
                "detail": {"latency_ms": 120.0, "token_units": 40,
                           "constraint_no_unverified_success": verdict == "pass"}}
    return run_case


async def _proposed(env, prompt_id="p-cmp"):
    exp = await env["service"].propose(
        kind=ExperimentKind.PROMPT, target=f"prompt:{prompt_id}",
        hypothesis="h", expected_benefit="b",
        baseline_ref="base-v1", candidate_ref="cand-v1",
        dataset_ref="ds", dataset={"cases": DATASET_CASES},
        thresholds=ExperimentThresholds(success_min=0.6,
                                        critical_constraints=("safe",)),
        proposer="operator:a")
    return exp


# ── §13.13: durable comparisons ────────────────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_comparison_requests_return_the_same_job(env):
    """§13.13 acceptance: the API's enqueue is create-or-get per pinned
    revision."""
    exp = await _proposed(env)
    revision_hash = json.dumps({"dataset": exp.dataset_hash}, sort_keys=True)
    first = await env["comparisons"].enqueue(
        exp.experiment_id, cases=DATASET_CASES, baseline_ref="base-v1",
        candidate_ref="cand-v1", revision_hash=revision_hash)
    second = await env["comparisons"].enqueue(
        exp.experiment_id, cases=DATASET_CASES, baseline_ref="base-v1",
        candidate_ref="cand-v1", revision_hash=revision_hash)
    assert first["comparison_id"] == second["comparison_id"]


@pytest.mark.asyncio
async def test_crash_midway_resumes_without_rewriting_evidence(env):
    """§13.13 acceptance: a crash halfway through comparison resumes —
    completed case records survive untouched and the experiment does not
    stay EVALUATING."""
    repo = env["comparisons"]
    comp = await repo.enqueue("exp-cmp", cases=DATASET_CASES,
                              baseline_ref="base-v1",
                              candidate_ref="cand-v1",
                              revision_hash="rh-1")
    await repo.claim("worker-A")
    # Worker A records ONE case then "dies" (lease expires).
    await repo.record_case(comp["comparison_id"], case_id="c1",
                           variant="baseline", verdict="pass")
    await env["db"].execute(
        "UPDATE experiment_comparisons SET lease_expires_at=1 WHERE "
        "comparison_id=?", (comp["comparison_id"],))
    await env["db"].commit()

    # Worker B resumes; c1/baseline must NOT be re-executed.
    executed = {"c1_baseline": 0}

    class TrackingExecutor:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, case, variant_ref):
            if case["case_id"] == "c1" and variant_ref == "base-v1":
                executed["c1_baseline"] += 1
            return await self.inner(case, variant_ref)

    base_executor = _executor()
    drained = await drain_comparisons(
        "worker-B", run_case=TrackingExecutor(base_executor),
        experiment_repo=env["repo"])
    assert drained == 1
    assert executed["c1_baseline"] == 0, "completed evidence is not rewritten"
    final = await repo.get(comp["comparison_id"])
    assert final["state"] == "completed"
    results = final["case_results"]
    assert len(results) == 6, "3 cases x 2 variants, all recorded"


@pytest.mark.asyncio
async def test_stale_worker_cannot_complete(env):
    repo = env["comparisons"]
    comp = await repo.enqueue("exp-cmp2", cases=DATASET_CASES,
                              baseline_ref="b", candidate_ref="c",
                              revision_hash="rh-2")
    await repo.claim("worker-A")
    from backend.services.experiment_comparison import ComparisonError
    with pytest.raises(ComparisonError, match="no longer yours"):
        await repo.complete(comp["comparison_id"], owner_worker_id="worker-B",
                            metrics={})


@pytest.mark.asyncio
async def test_metrics_match_the_typed_gate_shape(env):
    """§13.12/§13.13: the worker's metrics carry samples, success, and
    constraint flags — the shape the promotion gates consume."""
    repo = env["comparisons"]
    comp = await repo.enqueue("exp-cmp3", cases=DATASET_CASES,
                              baseline_ref="base-v1",
                              candidate_ref="cand-v1",
                              revision_hash="rh-3")
    await drain_comparisons("w1", run_case=_executor(pass_candidate=True),
                            experiment_repo=env["repo"])
    final = await repo.get(comp["comparison_id"])
    cand = final["results_json"] and json.loads(final["results_json"])["candidate"]
    assert cand["samples"] == 3
    assert 0.0 <= cand["success"] <= 1.0
    assert cand["constraint_no_unverified_success"] is True


# ── §13.16: measured shadow observations ───────────────────────────────

@pytest.mark.asyncio
async def test_shadow_observed_requires_recorded_candidate_execution(env):
    """THE §13.16 acceptance: 'shadow observed' = recorded candidate
    executions — outcome, latency, cost — never a state label."""
    repo = env["comparisons"]
    exp = await _proposed(env, prompt_id="p-shadow")
    await env["service"].evaluate(
        exp.experiment_id, evaluator="operator:b",
        run_evaluator=lambda v, d: {"success": 0.9, "samples": 5},
        dataset={"cases": DATASET_CASES})
    await env["service"].start_shadow(exp.experiment_id,
                                      activator="operator:c")
    # Enqueue the comparison, then run the candidate cases; SHADOW state
    # records measured observations.
    await env["comparisons"].enqueue(
        exp.experiment_id, cases=DATASET_CASES, baseline_ref="base-v1",
        candidate_ref="cand-v1", revision_hash="rh-shadow")
    await drain_comparisons(
        "w1", run_case=_executor(), experiment_repo=env["repo"])
    summary = await env["repo"].shadow_summary(exp.experiment_id)
    assert summary["observations"] == 3, "one per candidate case"
    assert summary["outcomes"].get("pass") == 3
    assert summary["mean_latency_ms"] == 120.0, "MEASURED latency recorded"
    assert summary["token_units_total"] == 120, "MEASURED cost recorded"
