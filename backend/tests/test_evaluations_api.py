"""Phase 12 §12: evaluations API — authorized retrieval of durable
evaluation records with tenant scoping and admin-only summary."""
import pytest

from backend.api.routers import evaluations as eval_router


class _Factory:
    def __init__(self, repo):
        self.evaluation_repo = repo


class _App:
    def __init__(self, repo):
        state = type("S", (), {})()
        state.factory = _Factory(repo)
        self.state = state


def _request(repo):
    return type("R", (), {"app": _App(repo)})()


def _user(uid="u1", role="user"):
    from backend.api.dependencies.auth import AuthenticatedUser
    return AuthenticatedUser(user_id=uid, email=f"{uid}@x.io", name=uid,
                             role=role, auth_provider="jwt")


@pytest.mark.asyncio
async def test_get_evaluation_returns_public_projection():
    from backend.services.evaluation_runner import evaluate_run
    raw = evaluate_run(run_id="run-api", workflow_id="wf-api", user_id="u1",
                       tasks=[{"task_id": "t1", "status": "COMPLETED",
                               "acceptance_criteria_json": [],
                               "output_data": {"reply": "done"}}],
                       model_name="m")

    class _Repo:
        async def get_evaluation(self, eid):
            assert eid == raw["evaluation_id"]
            return {"evaluation_id": eid, "run_id": "run-api",
                    "workflow_id": "wf-api", "agent_name": "orchestrator",
                    "model": "m", "success": 1, "recovery_attempts": 0,
                    "tool_calls": 0, "latency_ms": 0.0,
                    "token_usage_total": 0, "fixture_hash": raw["fixture_hash"],
                    "evaluator_version": raw["evaluator_version"],
                    "created_at_epoch": 1, "record": raw}

    out = await eval_router.get_evaluation(raw["evaluation_id"],
                                           _request(_Repo()), _user("u1"))
    assert out["evaluation_id"] == raw["evaluation_id"]
    assert out["success"] is True
    assert out["fixture_hash"] == raw["fixture_hash"]


@pytest.mark.asyncio
async def test_tenant_cannot_read_another_users_evaluation():
    raw_owner = {"user_id": "owner-9"}

    class _Repo:
        async def get_evaluation(self, eid):
            return {"evaluation_id": eid, "success": 1, "record": raw_owner}

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await eval_router.get_evaluation("eval_x", _request(_Repo()),
                                         _user("someone-else"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_summary_is_admin_only():
    from fastapi import HTTPException

    class _Repo:
        async def evaluation_summary(self):
            return {"total": 3, "success_rate": 0.67}

    with pytest.raises(HTTPException) as exc:
        await eval_router.evaluation_summary(_request(_Repo()), _user("u1"))
    assert exc.value.status_code == 403

    out = await eval_router.evaluation_summary(_request(_Repo()),
                                               _user("admin-1", role="admin"))
    assert out["total"] == 3


@pytest.mark.asyncio
async def test_unavailable_repo_is_503():
    from fastapi import HTTPException

    class _Factory:
        evaluation_repo = None

    class _App:
        state = type("S", (), {"factory": _Factory()})()

    req = type("R", (), {"app": _App()})()
    with pytest.raises(HTTPException) as exc:
        eval_router._repo(req)
    assert exc.value.status_code == 503
