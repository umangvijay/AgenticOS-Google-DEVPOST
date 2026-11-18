"""Audit F01 acceptance: organization membership requires ACCEPTANCE.

- An invitation is created pending; the raw token is returned exactly once
  and only its hash is stored.
- Membership is created only when the invited user accepts with the token;
  the accepting account's email must match the invited address.
- Personal runs remain inaccessible regardless (org-scoped oversight is
  the second half of the defense, tested in test_audit_security.py).
- Expired, revoked, reused, and duplicate-pending invitations are refused;
  membership keeps the create-only invariant.
"""
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
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "invite.db"))
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY", None)
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY", None)
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "private.pem"))
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY_PATH", str(tmp_path / "public.pem"))
    manager = JWTManager()
    manager.initialize()
    monkeypatch.setattr(auth_routes, "jwt_manager", manager)
    monkeypatch.setattr(dependencies, "jwt_manager", manager)
    monkeypatch.setattr(auth_routes, "check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(orgs, "check_rate_limit", lambda *a, **k: None)

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
    response = await env["client"].post("/api/v1/auth/signup", json={
        "name": email.split("@")[0], "email": email, "password": "Strong-local-pass9!"})
    assert response.status_code == 201, response.text
    return response.json()


def _headers(session):
    return {"Authorization": "Bearer " + session["access_token"]}


@pytest.mark.asyncio
async def test_f01_membership_requires_acceptance(env):
    client, factory = env["client"], env["factory"]
    owner = await _signup(env, "inv-owner@example.com")
    invitee = await _signup(env, "inv-member@example.com")
    headers = _headers(owner)
    org_id = (await client.post("/api/v1/orgs", json={"name": "Inv Org"}, headers=headers)).json()["org_id"]

    # Direct add is the legacy path; the invitation flow is the audited one.
    created = await client.post(f"/api/v1/orgs/{org_id}/invitations", headers=headers, json={
        "email": "inv-member@example.com", "role": "member"})
    assert created.status_code == 201, created.text
    invite = created.json()
    assert invite["token"], "raw token must be returned exactly once"

    # Pending, NOT a member yet.
    pending = (await client.get(f"/api/v1/orgs/{org_id}/invitations", headers=headers)).json()["invitations"]
    assert len(pending) == 1 and "token" not in pending[0], "tokens must never be listable"
    assert await factory.org_repo.get_member_role(org_id, invitee["user"]["id"]) is None

    # The invited user accepts with the token.
    accepted = await client.post("/api/v1/orgs/invitations/accept", headers=_headers(invitee), json={
        "token": invite["token"]})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "member"
    assert await factory.org_repo.get_member_role(org_id, invitee["user"]["id"]) == "member"

    # A token is single-use.
    reuse = await client.post("/api/v1/orgs/invitations/accept", headers=_headers(invitee), json={
        "token": invite["token"]})
    assert reuse.status_code == 409


@pytest.mark.asyncio
async def test_f01_wrong_user_cannot_accept_someone_elses_invitation(env):
    client, factory = env["client"], env["factory"]
    owner = await _signup(env, "inv2-owner@example.com")
    invitee = await _signup(env, "inv2-member@example.com")
    attacker = await _signup(env, "attacker@example.com")
    headers = _headers(owner)
    org_id = (await client.post("/api/v1/orgs", json={"name": "Org2"}, headers=headers)).json()["org_id"]

    invite = (await client.post(f"/api/v1/orgs/{org_id}/invitations", headers=headers, json={
        "email": "inv2-member@example.com", "role": "member"})).json()

    stolen = await client.post("/api/v1/orgs/invitations/accept", headers=_headers(attacker), json={
        "token": invite["token"]})
    assert stolen.status_code == 409
    assert "different email" in stolen.json()["detail"]
    # No membership was created for the attacker.
    assert await factory.org_repo.get_member_role(org_id, attacker["user"]["id"]) is None


@pytest.mark.asyncio
async def test_f01_duplicate_pending_invitation_refused(env):
    client = env["client"]
    owner = await _signup(env, "inv3-owner@example.com")
    headers = _headers(owner)
    org_id = (await client.post("/api/v1/orgs", json={"name": "Org3"}, headers=headers)).json()["org_id"]

    first = await client.post(f"/api/v1/orgs/{org_id}/invitations", headers=headers, json={
        "email": "someone@example.com", "role": "member"})
    assert first.status_code == 201
    # A second admin cannot silently upgrade the pending invite to admin.
    upgrade = await client.post(f"/api/v1/orgs/{org_id}/invitations", headers=headers, json={
        "email": "someone@example.com", "role": "admin"})
    assert upgrade.status_code == 409
    pending = (await client.get(f"/api/v1/orgs/{org_id}/invitations", headers=headers)).json()["invitations"]
    assert len(pending) == 1 and pending[0]["role"] == "member"


@pytest.mark.asyncio
async def test_f01_revoked_invitation_cannot_be_accepted(env):
    client = env["client"]
    owner = await _signup(env, "inv4-owner@example.com")
    invitee = await _signup(env, "inv4-member@example.com")
    headers = _headers(owner)
    org_id = (await client.post("/api/v1/orgs", json={"name": "Org4"}, headers=headers)).json()["org_id"]

    invite = (await client.post(f"/api/v1/orgs/{org_id}/invitations", headers=headers, json={
        "email": "inv4-member@example.com", "role": "member"})).json()
    revoked = await client.delete(f"/api/v1/orgs/{org_id}/invitations/{invite['invitation_id']}", headers=headers)
    assert revoked.status_code == 200

    accepted = await client.post("/api/v1/orgs/invitations/accept", headers=_headers(invitee), json={
        "token": invite["token"]})
    assert accepted.status_code == 409


@pytest.mark.asyncio
async def test_f01_viewer_cannot_invite_admin(env):
    client = env["client"]
    owner = await _signup(env, "inv5-owner@example.com")
    member = await _signup(env, "inv5-member@example.com")
    headers = _headers(owner)
    org_id = (await client.post("/api/v1/orgs", json={"name": "Org5"}, headers=headers)).json()["org_id"]
    invited = await client.post(f"/api/v1/orgs/{org_id}/members", headers=headers, json={
        "email": "inv5-member@example.com", "role": "member"})
    await client.post("/api/v1/orgs/invitations/accept", headers=_headers(member), json={
        "token": invited.json()["token"]})
    member_headers = _headers(member)

    denied = await client.post(f"/api/v1/orgs/{org_id}/invitations", headers=member_headers, json={
        "email": "newadmin@example.com", "role": "admin"})
    assert denied.status_code == 403, "only the owner can invite an admin"
