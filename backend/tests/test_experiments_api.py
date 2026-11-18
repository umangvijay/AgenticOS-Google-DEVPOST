"""Phase 13: experiments API — admin-gated retrieval and lifecycle with
separated permissions surfaced over HTTP."""
import json

import pytest
import pytest_asyncio

from backend.api.routers import experiments as exp_router
from backend.models.experiment import (
    ExperimentKind, ExperimentState, ExperimentThresholds)
from backend.repositories.experiment_repository import (
    ExperimentRepository, set_experiment_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.improvement_service import ImprovementService

DATASET = {"tasks": ["a", "b"]}


class _Factory:
    def __init__(self, db, repo):
        self.experiment_repo = repo
        # §13.19: activation needs the versioned prompt store.
        from backend.services.prompt_versions import PromptVersionStore
        self.prompt_version_store = PromptVersionStore(db) if db else None
        # §13.13: the durable comparison store.
        from backend.services.experiment_comparison import (
            ExperimentComparisonRepository)
        self.comparison_repo = (ExperimentComparisonRepository(db)
                                if db else None)


class _App:
    def __init__(self, db, repo):
        state = type("S", (), {})()
        state.factory = _Factory(db, repo) if db is not None else _Factory(None, repo)
        self.state = state


def _request(repo):
    # `repo` may be the env dict (with db) or a bare repo (older callers).
    if isinstance(repo, dict):
        return type("R", (), {"app": _App(repo["db"], repo["repo"])})()
    return type("R", (), {"app": _App(None, repo)})()


def _user(uid="admin-1", role="admin"):
    from backend.api.dependencies.auth import AuthenticatedUser
    return AuthenticatedUser(user_id=uid, email=f"{uid}@x.io", name=uid,
                             role=role, auth_provider="jwt")


@pytest_asyncio.fixture(autouse=True)
async def _enable_stages(monkeypatch):
    """§13.33: the API tests exercise every stage — enable them."""
    from backend.config.settings import settings as app_settings
    monkeypatch.setattr(app_settings, "IMPROVEMENT_STAGES_ENABLED",
                        "proposals,comparisons,shadow,canary,activation")


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "exp-api.db"))
    await db.initialize()
    repo = ExperimentRepository(db)
    set_experiment_repo(repo)
    try:
        yield {"db": db, "repo": repo}
    finally:
        set_experiment_repo(None)
        await db.close()


def _propose_body():
    return exp_router.ProposeBody(
        kind="prompt", target="prompt:summary", hypothesis="h",
        expected_benefit="b", baseline_ref="v3", candidate_ref="v4",
        dataset_ref="ds", dataset=DATASET,
        thresholds=ExperimentThresholds(success_min=0.8).to_dict())


@pytest.mark.asyncio
async def test_propose_and_get_round_trip(env):
    created = await exp_router.propose(_propose_body(), _request(env["repo"]),
                                       _user("admin-1"))
    assert created["state"] == "PROPOSED"
    assert created["dataset_hash"] and created["grading_hash"]

    fetched = await exp_router.get_experiment(created["experiment_id"],
                                              _request(env["repo"]),
                                              _user("admin-1"))
    assert fetched["experiment_id"] == created["experiment_id"]
    assert fetched["hypothesis"] == "h"


@pytest.mark.asyncio
async def test_non_admin_is_403_on_all_actions(env):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await exp_router.list_experiments(_request(env["repo"]), _user("u1", role="user"))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await exp_router.propose(_propose_body(), _request(env["repo"]),
                                 _user("u1", role="user"))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await exp_router.get_experiment("exp_x", _request(env["repo"]), _user("u1", role="user"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_lifecycle_over_http_and_evidence(env):
    # §13.19: the refs are REAL prompt-store versions — the coordinator
    # activates the candidate in the store on promotion.
    store = env["db"] and __import__(
        "backend.services.prompt_versions", fromlist=["PromptVersionStore"]
    ).PromptVersionStore(env["db"])
    baseline_v = await store.checkout("summary", "BASE {goal}", created_by="op:a")
    await store.activate("summary", baseline_v["version_id"], activated_by="op:a")
    candidate_v = await store.checkout("summary", "CAND {goal}", created_by="agent:x")
    svc = ImprovementService(env["repo"])
    exp = await svc.propose(
        kind=ExperimentKind.PROMPT, target="prompt:summary", hypothesis="h",
        expected_benefit="b", baseline_ref=baseline_v["version_id"],
        candidate_ref=candidate_v["version_id"],
        dataset_ref="ds", dataset=DATASET,
        thresholds=ExperimentThresholds(success_min=0.8),
        proposer="operator:alice")
    eid = exp.experiment_id
    await svc.evaluate(eid, evaluator="operator:bob",
                       run_evaluator=lambda v, d: {
                           "success": 0.9, "samples": 20,
                           "constraint_no_unauthorized_actions": True,
                           "constraint_no_unresolved_required_effects": True,
                           "constraint_no_unverified_success": True},
                       dataset=DATASET)
    req = _request(env)   # full env: prompt store available for activation
    await exp_router.start_shadow(eid, req, _user("admin-1"))
    await exp_router.start_canary(eid, exp_router.CanaryBody(cohort_size=5),
                                  req, _user("admin-1"))
    promoted = await exp_router.promote(
        eid, exp_router.PromoteBody(deployment_version="v9",
                                    total_population=100),
        req, _user("admin-1"))
    assert promoted["state"] == "PROMOTED"
    assert promoted["deployment_version"] == "v9"
    assert promoted["promoted_dataset_hash"] == exp.dataset_hash
    # §13.19: promotion through the API CHANGED THE EFFECTIVE RUNTIME.
    assert promoted["activation_receipt"]["state"] == "effective"
    active = await store.get_active("summary")
    assert active["version_id"] == candidate_v["version_id"]

    rolled = await exp_router.rollback(
        eid, exp_router.RollbackBody(reason="bad"), req,
        _user("admin-1"))
    assert rolled["state"] == "ROLLED_BACK"
    assert rolled["rollback_reason"] == "bad"
    # §13.20: the effective runtime REVERTED to the pinned baseline.
    assert rolled["rollback_confirmed"] is True
    assert (await store.get_active("summary"))["version_id"] == \
        baseline_v["version_id"]


@pytest.mark.asyncio
async def test_evaluation_requires_pinned_cases_and_returns_a_job(env):
    """§13.13: the evaluate endpoint enqueues a DURABLE comparison —
    with pinned cases it returns a job identity; without them it refuses
    (the API never invents evaluation inputs)."""
    created = await exp_router.propose(_propose_body(), _request(env),
                                       _user("admin-1"))
    from fastapi import HTTPException
    # Without cases: refused, not fabricated.
    with pytest.raises(HTTPException) as exc:
        await exp_router.evaluate(created["experiment_id"], _request(env),
                                  user=_user("admin-1"),
                                  body=exp_router.EvaluateBody())
    assert exc.value.status_code == 422
    # A SWAPPED case set is refused — the submitted cases must hash to
    # the dataset pinned at proposal (§14.J).
    with pytest.raises(HTTPException) as exc:
        await exp_router.evaluate(
            created["experiment_id"], _request(env), user=_user("admin-1"),
            body=exp_router.EvaluateBody(
                cases_json=json.dumps([{"case_id": "c1", "input": "x"}])))
    assert exc.value.status_code == 409
    # With the PINNED cases: a durable job identity comes back.
    out = await exp_router.evaluate(
        created["experiment_id"], _request(env), user=_user("admin-1"),
        body=exp_router.EvaluateBody(cases_json=json.dumps(DATASET)))
    assert out["comparison_id"].startswith("cmp_")
    assert out["state"] == "queued"
