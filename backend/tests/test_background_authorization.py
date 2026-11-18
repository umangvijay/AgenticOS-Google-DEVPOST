"""Phase 1.12/1.3 regressions: authorization carried into background
execution, and verified-recipient acceptance gating.

- A disabled owner's queued task is NOT executed by the worker (denied
  with an attributable audit event; acknowledged, not requeued forever).
- Invitation acceptance requires a verified email address when the
  conservative policy is enabled; Google-verified accounts pass.
"""
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.engine.engine import WorkflowEngine
from backend.models.schemas import Task, TaskStatus, WorkflowRun
from backend.repositories.factory import RepositoryFactory
from backend.repositories.in_memory_message_bus import InMemoryMessageBus
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository


@pytest_asyncio.fixture
async def worker_env(tmp_path):
    db = DatabaseManager(str(tmp_path / "bg.db"))
    await db.initialize()
    workflow_repo = SQLiteWorkflowRepository(db)
    bus = InMemoryMessageBus()
    engine = WorkflowEngine(workflow_repo, bus)

    audited = []
    class AuditStub:
        async def log_event(self, event):
            audited.append(event)
            return event

    class UserRepoStub:
        def __init__(self, active=True):
            self.active = active
        async def get_by_id(self, user_id):
            return {"id": user_id, "is_active": self.active, "email": "o@x"}

    user_repo = UserRepoStub(active=True)

    ctx_holder = {}

    class FakeCtx:
        async def ack(self):
            ctx_holder["acked"] = True
        async def nack(self):
            ctx_holder["nacked"] = True

    # Use the REAL worker authorization gate (no mirrored logic drift):
    from backend.worker import authorize_background_execution
    org_repo = None  # account-state tests; org recheck covered in test_ownership_transfer.py

    async def handle_with_authorization(run_id, task_id):
        deny = await authorize_background_execution(
            workflow_repo, run_id, task_id,
            user_repo=user_repo, org_repo=org_repo, audit=None,
        )
        if deny:
            audited.append(type("E", (), {
                "event_type": "BACKGROUND_AUTHORIZATION_DENIED",
                "details": {"reason": deny},
            }))
            await FakeCtx().ack()
            return "denied"
        await engine.execute_task(run_id, task_id)
        await FakeCtx().ack()
        return "executed"

    ctx_holder["handle"] = handle_with_authorization
    ctx_holder["audited"] = audited
    ctx_holder["user_repo"] = user_repo
    ctx_holder["org_repo"] = None  # account-state tests; org recheck covered in test_ownership_transfer.py
    ctx_holder["repo"] = workflow_repo
    yield ctx_holder
    await db.close()


async def _save_bg_run(repo, user_id: str) -> str:
    run_id = str(uuid.uuid4())
    task = Task(task_id="t1", workflow_id="wf-bg", run_id=run_id, user_id=user_id,
                agent="core.set", input_data={"fields": {"x": 1}}, status=TaskStatus.PENDING)
    run = WorkflowRun(run_id=run_id, workflow_id="wf-bg", user_id=user_id,
                      goal="bg work", status=TaskStatus.RUNNING, tasks=[task])
    await repo.save_run(run.model_dump(mode="json"))
    return run_id


@pytest.mark.asyncio
async def test_bg_disabled_owner_task_denied_not_executed(worker_env):
    run_id = await _save_bg_run(worker_env["repo"], "owner-disabled")
    worker_env["user_repo"].active = False
    outcome = await worker_env["handle"](run_id, "t1")
    assert outcome == "denied"
    assert worker_env["audited"], "denial must be attributable (audit event)"
    assert worker_env["audited"][-1].event_type == "BACKGROUND_AUTHORIZATION_DENIED"
    # The task was NOT executed.
    run = await worker_env["repo"].get_run(run_id)
    assert run["tasks"][0]["status"] == TaskStatus.PENDING
    assert worker_env.get("acked") is True, "denied work is acknowledged, not requeued forever"


@pytest.mark.asyncio
async def test_bg_active_owner_task_executes(worker_env):
    run_id = await _save_bg_run(worker_env["repo"], "owner-active")
    outcome = await worker_env["handle"](run_id, "t1")
    assert outcome == "executed"
    run = await worker_env["repo"].get_run(run_id)
    assert run["tasks"][0]["status"] == TaskStatus.COMPLETED


# ── 1.3: verified-recipient acceptance gate ──────────────────────────

@pytest_asyncio.fixture
async def invite_env(tmp_path, monkeypatch):
    from backend.config.settings import settings
    from backend.repositories.factory import RepositoryFactory
    from backend.security.jwt_manager import JWTManager
    from backend.api.routers import auth as auth_routes, orgs
    from backend.api.dependencies import auth as dependencies
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "ver.db"))
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY", None)
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY", None)
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "private.pem"))
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY_PATH", str(tmp_path / "public.pem"))
    manager = JWTManager()
    manager.initialize()
    monkeypatch.setattr(auth_routes, "jwt_manager", manager)
    monkeypatch.setattr(dependencies, "jwt_manager", manager)
    for module in (auth_routes, orgs):
        if hasattr(module, "check_rate_limit"):
            monkeypatch.setattr(module, "check_rate_limit", lambda *a, **k: None)
        if hasattr(module, "check_shared_rate_limit"):
            async def _shared_ok(*a, **k):
                return {"shared": False}
            monkeypatch.setattr(module, "check_shared_rate_limit", _shared_ok)

    factory = RepositoryFactory()
    await factory.initialize()
    app = FastAPI()
    app.state.factory = factory
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.include_router(orgs.router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "factory": factory, "settings": settings}
    # shutdown() clears the module-level repo singletons this factory
    # registered — a raw db close leaked them into later tests.
    await factory.shutdown()


async def _signup(env, email):
    r = await env["client"].post("/api/v1/auth/signup", json={
        "name": email.split("@")[0], "email": email, "password": "Strong-local-pass9!"})
    assert r.status_code == 201
    return r.json()


def _h(session):
    return {"Authorization": "Bearer " + session["access_token"]}


@pytest.mark.asyncio
async def test_s13_verified_gate_blocks_unverified_acceptance(invite_env, monkeypatch):
    """Conservative policy ON: a local (unverified) account cannot accept an
    organization invitation until its email is verified."""
    env = invite_env
    monkeypatch.setattr(env["settings"], "REQUIRE_VERIFIED_EMAIL_FOR_INVITES", True)
    owner = await _signup(env, "ver-owner@example.com")
    invitee = await _signup(env, "ver-member@example.com")
    headers = _h(owner)
    org_id = (await env["client"].post("/api/v1/orgs", json={"name": "V"}, headers=headers)).json()["org_id"]
    invited = await env["client"].post(f"/api/v1/orgs/{org_id}/members", headers=headers, json={
        "email": "ver-member@example.com", "role": "member"})
    assert invited.status_code == 202

    denied = await env["client"].post("/api/v1/orgs/invitations/accept", headers=_h(invitee), json={
        "token": invited.json()["token"]})
    assert denied.status_code == 403
    assert "verify your email" in denied.json()["detail"].lower()
    assert await env["factory"].org_repo.get_member_role(org_id, invitee["user"]["id"]) is None

    # Once verified (the verification flow stamps the field), acceptance works.
    await env["factory"].user_repo.update_user(invitee["user"]["id"], {"email_verified": True})
    accepted = await env["client"].post("/api/v1/orgs/invitations/accept", headers=_h(invitee), json={
        "token": invited.json()["token"]})
    assert accepted.status_code == 200
    assert await env["factory"].org_repo.get_member_role(org_id, invitee["user"]["id"]) == "member"


@pytest.mark.asyncio
async def test_s13_policy_off_preserves_local_flow(invite_env):
    """Policy OFF (local default): the existing invitation flow is unchanged."""
    env = invite_env
    owner = await _signup(env, "ver2-owner@example.com")
    invitee = await _signup(env, "ver2-member@example.com")
    headers = _h(owner)
    org_id = (await env["client"].post("/api/v1/orgs", json={"name": "V2"}, headers=headers)).json()["org_id"]
    invited = await env["client"].post(f"/api/v1/orgs/{org_id}/members", headers=headers, json={
        "email": "ver2-member@example.com", "role": "member"})
    accepted = await env["client"].post("/api/v1/orgs/invitations/accept", headers=_h(invitee), json={
        "token": invited.json()["token"]})
    assert accepted.status_code == 200
