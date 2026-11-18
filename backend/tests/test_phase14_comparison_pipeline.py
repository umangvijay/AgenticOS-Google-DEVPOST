"""Phase 14 §14.6: the DURABLE comparison pipeline — create-or-get
identity, per-case resume, the EVALUATED advance, and measured shadow
observations. The executor is deployment-wired: without one the job
QUEUES (the pipeline never fabricates numbers)."""
import json

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "cmp14.db"))
    await db.initialize()
    from backend.services.experiment_comparison import (
        ExperimentComparisonRepository, set_comparison_repo)
    repo = ExperimentComparisonRepository(db)
    set_comparison_repo(repo)
    try:
        yield {"db": db, "repo": repo}
    finally:
        set_comparison_repo(None)
        await db.close()


def _cases(n=4):
    return [{"case_id": f"c{i}", "input": f"in-{i}"} for i in range(n)]


@pytest.mark.asyncio
async def test_without_executor_the_job_queues_never_fabricates(env):
    """No deployment-wired run_case → the claim returns nothing and no
    results appear. The pipeline cannot invent evaluation numbers."""
    comp = await env["repo"].enqueue(
        "exp-14.6", cases=_cases(), baseline_ref="v1", candidate_ref="v2",
        revision_hash=json.dumps({"rev": 1}, sort_keys=True))
    from backend.services.experiment_comparison import drain_comparisons
    drained = await drain_comparisons("w1", run_case=None,
                                      experiment_repo=None)
    assert drained == 0
    got = await env["repo"].get(comp["comparison_id"])
    assert got["state"] == "queued"


@pytest.mark.asyncio
async def test_cases_execute_for_both_variants_and_complete(env):
    """Every pinned case runs for BOTH variants; the comparison completes
    with §13.12-shaped metrics."""
    comp = await env["repo"].enqueue(
        "exp-14.6b", cases=_cases(3), baseline_ref="v1", candidate_ref="v2",
        revision_hash=json.dumps({"rev": 2}, sort_keys=True))

    async def run_case(case, variant_ref):
        return {"verdict": "pass" if variant_ref == "v2" else "pass",
                "detail": {"case": case["case_id"],
                           "variant": variant_ref}}

    from backend.services.experiment_comparison import drain_comparisons
    drained = await drain_comparisons("w1", run_case=run_case,
                                      experiment_repo=None)
    assert drained == 1
    got = await env["repo"].get(comp["comparison_id"])
    results = got["case_results"]
    # 3 cases × 2 variants, each recorded exactly once.
    pairs = {(r["case_id"], r["variant"]) for r in results}
    assert len(pairs) == 6 and len(results) == 6
    assert got["state"] == "completed"


@pytest.mark.asyncio
async def test_resume_never_rewrites_recorded_evidence(env):
    """A crash mid-comparison: the retry re-executes ONLY the missing
    (case, variant) pairs — completed evidence is never rewritten."""
    comp = await env["repo"].enqueue(
        "exp-14.6c", cases=_cases(2), baseline_ref="v1", candidate_ref="v2",
        revision_hash=json.dumps({"rev": 3}, sort_keys=True))
    # Simulate a crash after ONE case ran for the baseline only.
    await env["repo"].record_case(
        comp["comparison_id"], case_id="c0", variant="baseline",
        verdict="pass", detail={})
    executed = []
    async def run_case(case, variant_ref):
        executed.append((case["case_id"], variant_ref))
        return {"verdict": "pass", "detail": {}}
    from backend.services.experiment_comparison import drain_comparisons
    await drain_comparisons("w1", run_case=run_case, experiment_repo=None)
    assert ("c0", "v1") not in executed, \
        "completed evidence is never re-executed"
    assert ("c1", "v1") in executed and ("c1", "v2") in executed


@pytest.mark.asyncio
async def test_create_or_get_identity_on_revision_hash(env):
    """The same pinned comparison returns the SAME job — a repeated
    enqueue cannot fork the evidence."""
    first = await env["repo"].enqueue(
        "exp-14.6d", cases=_cases(2), baseline_ref="v1", candidate_ref="v2",
        revision_hash=json.dumps({"rev": 4}, sort_keys=True))
    second = await env["repo"].enqueue(
        "exp-14.6d", cases=_cases(2), baseline_ref="v1", candidate_ref="v2",
        revision_hash=json.dumps({"rev": 4}, sort_keys=True))
    assert first["comparison_id"] == second["comparison_id"]
    # A DIFFERENT revision hash is a NEW job (the pin changed).
    third = await env["repo"].enqueue(
        "exp-14.6d", cases=_cases(2), baseline_ref="v1", candidate_ref="v3",
        revision_hash=json.dumps({"rev": 5}, sort_keys=True))
    assert third["comparison_id"] != first["comparison_id"]
