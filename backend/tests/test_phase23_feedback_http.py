"""Feedback routes against a real SQLite store; only identity is injected."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
from backend.api.routers.feedback import router
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.feedback_repository import SQLiteFeedbackRepository
from backend.security.shared_rate_limiter import SQLiteSharedRateRepository


@pytest_asyncio.fixture
async def stack(tmp_path):
    db = DatabaseManager(str(tmp_path / "feedback.db"))
    await db.initialize()
    repo = SQLiteFeedbackRepository(db)
    app = FastAPI(version="candidate-test-version")
    identity = {"user_id": "owner", "role": "user"}
    async def actor():
        return AuthenticatedUser(identity["user_id"], "test@example.com", "Test",
                                 identity["role"], "local")
    app.dependency_overrides[get_current_user] = actor
    app.state.factory = SimpleNamespace(
        feedback_repo=repo, shared_rate_repo=SQLiteSharedRateRepository(db),
        workflow_repo=SimpleNamespace(get_run=AsyncMock(return_value={
            "run_id": "run", "user_id": "owner", "tasks": [{"task_id": "task"}],
            "model_version": "actual-model", "policy_version": "policy-3"})),
        mcp_repo=SimpleNamespace(get_mcp=AsyncMock(return_value={"owner": "owner"})))
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, repo, identity, app
    await db.close()


def payload(**extra):
    return {"helpful": False, "category": "incorrect_result", "run_id": "run", **extra}


@pytest.mark.asyncio
async def test_feedback_versions_consent_triage_and_deletion(stack):
    client, repo, identity, _ = stack
    response = await client.post("/feedback", json=payload(task_id="task", comment="wrong total"))
    assert response.status_code == 201
    fid = response.json()["feedback_id"]
    stored = await repo.get(fid)
    assert stored["model_version"] == "actual-model"
    assert stored["policy_version"] == "policy-3"
    assert stored["app_version"] == "candidate-test-version"
    assert (await client.get("/feedback")).status_code == 403
    identity["role"] = "admin"
    report = (await client.get("/feedback")).json()["feedback"][0]
    assert report["run_id"] is None and report["task_id"] is None
    assert (await client.post(f"/feedback/{fid}/triage", json={"status": "investigating"})).status_code == 200
    identity["role"] = "user"
    mine = (await client.get("/feedback/mine?run_id=run")).json()["feedback"]
    assert mine[0]["triage_status"] == "investigating"
    identity["user_id"] = "stranger"
    assert (await client.delete(f"/feedback/{fid}")).status_code == 404
    assert (await client.get("/feedback/mine")).json()["feedback"] == []
    identity["user_id"] = "owner"
    assert (await client.delete(f"/feedback/{fid}")).status_code == 200
    assert await repo.get(fid) is None


@pytest.mark.asyncio
async def test_cross_owner_references_and_invalid_task_are_refused(stack):
    client, _, identity, _ = stack
    identity["user_id"] = "stranger"
    assert (await client.post("/feedback", json=payload())).status_code == 404
    assert (await client.post("/feedback", json=payload(run_id=None, tool_id="tool"))).status_code == 404
    identity["user_id"] = "owner"
    assert (await client.post("/feedback", json=payload(task_id="other"))).status_code == 404
    assert (await client.post("/feedback", json=payload(run_id=None, task_id="task"))).status_code == 400


@pytest.mark.asyncio
async def test_shared_admission_caps_concurrent_feedback_and_fails_closed(stack):
    client, repo, _, app = stack
    responses = await asyncio.gather(*[
        client.post("/feedback", json=payload()) for _ in range(55)])
    assert sum(r.status_code == 201 for r in responses) == 50
    denied = [r for r in responses if r.status_code == 429]
    assert len(denied) == 5 and all(int(r.headers["Retry-After"]) > 0 for r in denied)
    assert len(await repo.list_for_user("owner", limit=100)) == 50
    app.state.factory.shared_rate_repo = None
    assert (await client.post("/feedback", json=payload())).status_code == 503
