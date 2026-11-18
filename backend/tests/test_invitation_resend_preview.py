"""Phase 1.5/1.7/1.13 regressions: resend policy, invitation preview, and
org-existence revalidation at acceptance."""
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    from backend.config.settings import settings
    from backend.repositories.factory import RepositoryFactory
    from backend.security.jwt_manager import JWTManager
    from backend.api.routers import auth as auth_routes, orgs
    from backend.api.dependencies import auth as dependencies

    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "phase17.db"))
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
            async def _shared(*a, **k):
                return {"shared": False}
            monkeypatch.setattr(module, "check_shared_rate_limit", _shared)

    factory = RepositoryFactory()
    await factory.initialize()
    app = FastAPI()
    app.state.factory = factory
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.include_router(orgs.router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "factory": factory}
    await factory.db_manager.close()


async def _signup(env, email):
    r = await env["client"].post("/api/v1/auth/signup", json={
        "name": email.split("@")[0], "email": email, "password": "Strong-local-pass9!"})
    assert r.status_code == 201
    return r.json()


def _h(session):
    return {"Authorization": "Bearer " + session["access_token"]}


# ── 1.5: resend policy ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_p15_resend_rotates_token_and_preserves_role(env):
    client = env["client"]
    owner = await _signup(env, "rs-owner@example.com")
    headers = _h(owner)
    org_id = (await client.post("/api/v1/orgs", json={"name": "RS"}, headers=headers)).json()["org_id"]
    invited = (await client.post(f"/api/v1/orgs/{org_id}/invitations", headers=headers, json={
        "email": "rs-member@example.com", "role": "member"})).json()

    resent = await client.post(f"/api/v1/orgs/{org_id}/invitations/{invited['invitation_id']}/resend",
                               headers=headers)
    assert resent.status_code == 200, resent.text
    body = resent.json()
    assert body["token"] != invited["token"], "resend must rotate the token"
    assert body["role"] == "member", "resend must preserve the authorized role"
    assert body["email"] == "rs-member@example.com"

    # The OLD token is dead (single live token per invitation).
    old_accept = await client.post("/api/v1/orgs/invitations/accept", headers=_h(owner), json={
        "token": invited["token"]})
    assert old_accept.status_code == 409
    # The NEW token works.
    invitee = await _signup(env, "rs-member@example.com")
    accepted = await client.post("/api/v1/orgs/invitations/accept", headers=_h(invitee), json={
        "token": body["token"]})
    assert accepted.status_code == 200
    assert accepted.json()["role"] == "member"


@pytest.mark.asyncio
async def test_p15_admin_cannot_resend_an_admin_invitation(env):
    client = env["client"]
    owner = await _signup(env, "rs2-owner@example.com")
    admin_session = await _signup(env, "rs2-admin@example.com")
    headers = _h(owner)
    org_id = (await client.post("/api/v1/orgs", json={"name": "RS2"}, headers=headers)).json()["org_id"]
    admin_invite = (await client.post(f"/api/v1/orgs/{org_id}/invitations", headers=headers, json={
        "email": "rs2-admin@example.com", "role": "admin"})).json()
    await client.post("/api/v1/orgs/invitations/accept", headers=_h(admin_session), json={
        "token": admin_invite["token"]})
    # Owner invites a NEW admin; the (now) admin tries to resend it.
    second = (await client.post(f"/api/v1/orgs/{org_id}/invitations", headers=headers, json={
        "email": "rs2-second@example.com", "role": "admin"})).json()
    denied = await client.post(
        f"/api/v1/orgs/{org_id}/invitations/{second['invitation_id']}/resend",
        headers=_h(admin_session))
    assert denied.status_code == 403, "only the owner may resend an admin's invitation"


# ── 1.13: invitation preview ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_p13_preview_shows_org_and_role_without_mutating(env):
    client = env["client"]
    owner = await _signup(env, "pv-owner@example.com")
    invitee = await _signup(env, "pv-member@example.com")
    headers = _h(owner)
    org_id = (await client.post("/api/v1/orgs", json={"name": "Preview Org"}, headers=headers)).json()["org_id"]
    invited = (await client.post(f"/api/v1/orgs/{org_id}/invitations", headers=headers, json={
        "email": "pv-member@example.com", "role": "member"})).json()

    preview = await client.post("/api/v1/orgs/invitations/preview", headers=_h(invitee), json={
        "token": invited["token"]})
    assert preview.status_code == 200
    body = preview.json()
    assert body["org_name"] == "Preview Org"
    assert body["role"] == "member"
    assert body["org_exists"] is True
    # Preview never flips status or creates membership.
    pending = (await client.get(f"/api/v1/orgs/{org_id}/invitations", headers=headers)).json()["invitations"]
    assert len(pending) == 1 and pending[0]["status"] == "pending"
    assert "token" not in preview.json()

    # The token still works after previewing.
    accepted = await client.post("/api/v1/orgs/invitations/accept", headers=_h(invitee), json={
        "token": invited["token"]})
    assert accepted.status_code == 200
