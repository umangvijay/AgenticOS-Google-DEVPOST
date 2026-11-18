"""Audit first-batch acceptance (F01–F04): adversarial authorization regressions.

Each test encodes an acceptance criterion from the 30-Oct audit. These are
the defenses against the three reproduced defects plus revocation semantics:

- F01: organization oversight never exposes personal runs — only runs
  explicitly created with the organization's org_id.
- F02: membership creation never mutates an existing member's role — at the
  router AND the repository (atomic create-only insert).
- F03: disabled accounts receive no tokens via password or Google login.
- F04: revocation fails closed, and tokens carry current account state.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def factory(tmp_path, monkeypatch):
    from backend.config.settings import settings
    from backend.repositories.factory import RepositoryFactory
    from backend.security.jwt_manager import JWTManager

    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY", None)
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY", None)
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "private.pem"))
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY_PATH", str(tmp_path / "public.pem"))
    manager = JWTManager()
    manager.initialize()
    monkeypatch.setattr(settings, "_jwt_manager", manager, raising=False)
    from backend.api.routers import auth as auth_routes
    from backend.api.dependencies import auth as dependencies
    monkeypatch.setattr(auth_routes, "jwt_manager", manager)
    monkeypatch.setattr(dependencies, "jwt_manager", manager)
    factory = RepositoryFactory()
    await factory.initialize()
    yield factory
    await factory.db_manager.close()


@pytest_asyncio.fixture
async def app(factory):
    from backend.api.routers import auth as auth_routes, orgs

    application = FastAPI()
    application.state.factory = factory
    application.include_router(auth_routes.router, prefix="/api/v1")
    application.include_router(orgs.router, prefix="/api/v1")
    return application


@pytest_asyncio.fixture
async def client(app):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _seed_user(factory, email: str, role: str = "user", active: bool = True) -> dict:
    user_id = str(uuid.uuid4())
    await factory.user_repo.create_user({
        "id": user_id,
        "email": email,
        "name": email.split("@")[0],
        "password_hash": "$2b$12$C6UzMDM.H6dfI/f/IKcEeO7ZbnCuE1AwUJNv9nx3rQ1fHOfC0KfXu",  # 'password'
        "role": role,
        "is_active": active,
        "auth_provider": "local",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"id": user_id, "email": email}


async def _signup(client, email: str, password: str = "Strong-local-pass9!") -> dict:
    response = await client.post("/api/v1/auth/signup", json={
        "name": email.split("@")[0], "email": email, "password": password,
    })
    assert response.status_code == 201, response.text
    return response.json()


def _auth_headers(session: dict) -> dict:
    return {"Authorization": "Bearer " + session["access_token"]}


# ── F02: membership creation never mutates roles ──────────────────────

@pytest.mark.asyncio
async def test_f02_repository_duplicate_add_never_mutates_role(factory):
    """Audit acceptance: repeated add-member requests cannot change roles."""
    owner = await _seed_user(factory, "owner-f02@example.com")
    org = await factory.org_repo.create_org("F02 Org", owner["id"])
    member = await _seed_user(factory, "member-f02@example.com")

    assert await factory.org_repo.add_member(org["org_id"], member["id"], "admin", owner["id"]) is True
    # Re-adding with a DIFFERENT role must be a no-op, not an overwrite.
    assert await factory.org_repo.add_member(org["org_id"], member["id"], "viewer", owner["id"]) is False
    assert await factory.org_repo.get_member_role(org["org_id"], member["id"]) == "admin"


@pytest.mark.asyncio
async def test_f02_router_readd_returns_409_and_role_holds(client, factory):
    """Audit acceptance: an admin cannot demote/promote another admin via any
    add endpoint. The members route creates an INVITATION (202) — never a
    membership; a duplicate pending invite is 409; roles change only through
    the owner-only role endpoint after acceptance."""
    owner_session = await _signup(client, "owner-f02b@example.com")
    owner_id = owner_session["user"]["id"]
    member_session = await _signup(client, "member-f02b@example.com")
    member_id = member_session["user"]["id"]
    headers = _auth_headers(owner_session)

    created = await client.post("/api/v1/orgs", json={"name": "F02B Org"}, headers=headers)
    org_id = created.json()["org_id"]

    first = await client.post(f"/api/v1/orgs/{org_id}/members", headers=headers, json={
        "email": "member-f02b@example.com", "role": "admin"})
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "invitation_pending"
    # No membership exists before acceptance.
    assert await factory.org_repo.get_member_role(org_id, member_id) is None

    replay = await client.post(f"/api/v1/orgs/{org_id}/members", headers=headers, json={
        "email": "member-f02b@example.com", "role": "viewer"})
    assert replay.status_code == 409, replay.text
    assert await factory.org_repo.get_member_role(org_id, member_id) is None

    # Accept → member with the invited role.
    accepted = await client.post("/api/v1/orgs/invitations/accept", headers=_auth_headers(member_session), json={
        "token": first.json()["token"]})
    assert accepted.status_code == 200
    assert await factory.org_repo.get_member_role(org_id, member_id) == "admin"

    detail = await client.get(f"/api/v1/orgs/{org_id}", headers=headers)
    roles = {m["user_id"]: m["role"] for m in detail.json()["members"]}
    assert roles[member_id] == "admin"


# ── F03: disabled accounts get no tokens on any login path ────────────

@pytest.mark.asyncio
async def test_f03_disabled_password_login_rejected(client, factory):
    session = await _signup(client, "disabled-f03@example.com")
    await factory.user_repo.set_user_active(session["user"]["id"], False)
    response = await client.post("/api/v1/auth/login", json={
        "email": "disabled-f03@example.com", "password": "Strong-local-pass9!"})
    assert response.status_code == 401
    assert "access_token" not in response.json()


@pytest.mark.asyncio
async def test_f03_disabled_google_user_gets_no_tokens(client, factory, monkeypatch):
    """Audit F03: the Google path must enforce active-account status too."""
    from backend.api.routers import auth as routes

    google_user = {"google_id": "gid-f03", "email": "g-f03@example.com", "name": "G User"}
    monkeypatch.setattr(routes, "verify_google_id_token", lambda *a, **k: google_user)
    monkeypatch.setattr(routes, "check_rate_limit", lambda *a, **k: None)

    # Existing Google-provider user, disabled.
    uid = str(uuid.uuid4())
    await factory.user_repo.create_user({
        "id": uid, "email": google_user["email"], "name": google_user["name"],
        "auth_provider": "google", "google_id": google_user["google_id"],
        "role": "user", "is_active": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    response = await client.post("/api/v1/auth/google", json={"id_token": "fake-token"})
    assert response.status_code == 401
    assert "access_token" not in response.json()


# ── F04: revocation enforced, fails closed, current account state ─────

@pytest.mark.asyncio
async def test_f04_revoked_token_rejected_and_denylist_failure_fails_closed(app, factory, monkeypatch):
    from backend.api.dependencies import auth as dependencies
    from httpx import ASGITransport, AsyncClient

    session_payload = {
        "name": "Revoke User", "email": "revoke-f04@example.com", "password": "Strong-local-pass9!",
    }
    # Signup through the app so the denylist repo is wired to the same factory.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        signup = await client.post("/api/v1/auth/signup", json=session_payload)
        assert signup.status_code == 201
        session = signup.json()
        headers = _auth_headers(session)
        assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200

        # Logout revokes the jti server-side.
        logout = await client.post("/api/v1/auth/logout", headers=headers)
        assert logout.status_code in (200, 204)
        assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401

    # A denylist outage must FAIL CLOSED (503), never allow the request through.
    broken = AsyncMock(side_effect=RuntimeError("denylist storage down"))
    monkeypatch.setattr(factory.token_denylist_repo, "is_revoked", broken)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/auth/me", headers=_auth_headers(session))
        assert response.status_code == 503, response.text


@pytest.mark.asyncio
async def test_f04_demotion_uses_current_account_state_not_claims(client, factory):
    """Audit acceptance: demoted administrators retain no claim permissions."""
    session = await _signup(client, "demote-f04@example.com")
    headers = _auth_headers(session)
    uid = session["user"]["id"]
    # Role arrives from claims at issuance; the dependency must re-read state.
    await factory.user_repo.update_user(uid, {"role": "admin"})
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()["role"] == "admin"
    await factory.user_repo.update_user(uid, {"role": "viewer"})
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()["role"] == "viewer"


# ── F01: oversight never exposes personal runs ────────────────────────

async def _persist_run(factory, user_id: str, goal: str, org_id: str | None = None) -> str:
    run_id = str(uuid.uuid4())
    await factory.workflow_repo.save_run({
        "run_id": run_id,
        "workflow_id": f"wf-{run_id[:8]}",
        "user_id": user_id,
        "goal": goal,
        "status": "COMPLETED",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "org_id": org_id,
        "tasks": [{"task_id": "t1", "agent": "core.set", "status": "COMPLETED",
                   "input_data": {"secret": "member-private"}, "output_data": {"secret": "member-private"}}],
    })
    return run_id


@pytest.mark.asyncio
async def test_f01_oversight_excludes_personal_runs_and_shows_only_org_owned(client, factory):
    """Audit acceptance: personal runs remain inaccessible before and after
    membership; only explicitly organization-owned runs appear."""
    owner_session = await _signup(client, "owner-f01@example.com")
    owner_id = owner_session["user"]["id"]
    member_session = await _signup(client, "member-f01@example.com")
    member_id = member_session["user"]["id"]
    headers = _auth_headers(owner_session)

    created = await client.post("/api/v1/orgs", json={"name": "F01 Org"}, headers=headers)
    org_id = created.json()["org_id"]

    # BEFORE membership: the member's personal runs exist but are nobody
    # else's business.
    personal_a = await _persist_run(factory, member_id, "Personal goal A — my private data")
    personal_b = await _persist_run(factory, member_id, "Personal goal B — also private")

    added = await client.post(f"/api/v1/orgs/{org_id}/members", headers=headers, json={
        "email": "member-f01@example.com", "role": "member"})
    assert added.status_code == 202, added.text
    accepted = await client.post("/api/v1/orgs/invitations/accept", headers=_auth_headers(member_session), json={
        "token": added.json()["token"]})
    assert accepted.status_code == 200, accepted.text

    # AFTER membership: oversight must still not surface the personal runs.
    oversight = await client.get(f"/api/v1/orgs/{org_id}/runs", headers=headers)
    assert oversight.status_code == 200
    body = oversight.json()
    assert body["runs"] == [], body
    assert "Personal goal A" not in oversight.text and "Personal goal B" not in oversight.text

    # A run explicitly created as organization-owned IS visible, with
    # task payloads sanitized to safe fields.
    org_run = await _persist_run(factory, member_id, "Org goal — shared work", org_id=org_id)
    oversight = await client.get(f"/api/v1/orgs/{org_id}/runs", headers=headers)
    runs = oversight.json()["runs"]
    assert [r["run_id"] for r in runs] == [org_run]
    assert runs[0]["member_email"] == "member-f01@example.com"
    assert all("secret" not in (t.get("input_data") or {}) for t in runs[0]["tasks"])
    assert all("secret" not in (t.get("output_data") or {}) for t in runs[0]["tasks"])
    assert "member-private" not in oversight.text

    # Leaving the org removes access again.
    await factory.org_repo.remove_member(org_id, member_id)
    oversight = await client.get(f"/api/v1/orgs/{org_id}/runs", headers=headers)
    assert [r["run_id"] for r in oversight.json()["runs"]] == []

    # The personal run ids are never leaked in any response above.
    assert personal_a not in oversight.text and personal_b not in oversight.text


@pytest.mark.asyncio
async def test_f01_other_org_runs_never_leak(client, factory):
    """A second organization's owned runs must not appear in this org's oversight."""
    owner_session = await _signup(client, "owner-f01b@example.com")
    other_session = await _signup(client, "owner-f01c@example.com")
    headers = _auth_headers(owner_session)

    org_a = (await client.post("/api/v1/orgs", json={"name": "Org A"}, headers=headers)).json()["org_id"]
    other_headers = _auth_headers(other_session)
    org_b = (await client.post("/api/v1/orgs", json={"name": "Org B"}, headers=other_headers)).json()["org_id"]

    await _persist_run(factory, other_session["user"]["id"], "Org B work", org_id=org_b)
    oversight = await client.get(f"/api/v1/orgs/{org_a}/runs", headers=headers)
    assert oversight.json()["runs"] == []
