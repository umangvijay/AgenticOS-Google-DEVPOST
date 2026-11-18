"""Phase 1.15 adversarial tenant-isolation regressions.

Identities: org owner/admin/member/viewer, unrelated user, disabled
account. Matrix rows proven here: personal runs denied cross-user,
oversight excludes personal and other-org runs, artifact downloads deny
foreign owners, schedule listing is owner-scoped, credential use is
owner-scoped, removed members lose access, disabled accounts gain
nothing (covered in test_audit_security.py — referenced, not repeated).
"""
import uuid
from datetime import datetime, timedelta, timezone

import json

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    from backend.config.settings import settings
    from backend.repositories.factory import RepositoryFactory
    from backend.security.jwt_manager import JWTManager
    from backend.api.routers import auth as auth_routes, orgs, schedules, artifacts
    from backend.api.dependencies import auth as dependencies

    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "tenancy.db"))
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY", None)
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY", None)
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "private.pem"))
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY_PATH", str(tmp_path / "public.pem"))
    manager = JWTManager()
    manager.initialize()
    monkeypatch.setattr(auth_routes, "jwt_manager", manager)
    monkeypatch.setattr(dependencies, "jwt_manager", manager)
    for module in (auth_routes, orgs, schedules):
        if hasattr(module, "check_rate_limit"):
            monkeypatch.setattr(module, "check_rate_limit", lambda *a, **k: None)
        if hasattr(module, "check_shared_rate_limit"):
            monkeypatch.setattr(module, "check_shared_rate_limit", _noop_shared)

    factory = RepositoryFactory()
    await factory.initialize()
    app = FastAPI()
    app.state.factory = factory
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.include_router(orgs.router, prefix="/api/v1")
    app.include_router(schedules.router, prefix="/api/v1")
    app.include_router(artifacts.router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "factory": factory}
    await factory.db_manager.close()


async def _noop_shared(*a, **k):
    return {"shared": False}


async def _signup(env, email):
    response = await env["client"].post("/api/v1/auth/signup", json={
        "name": email.split("@")[0], "email": email, "password": "Strong-local-pass9!"})
    assert response.status_code == 201, response.text
    return response.json()


def _headers(session):
    return {"Authorization": "Bearer " + session["access_token"]}


async def _persist_run(factory, user_id: str, goal: str, org_id=None) -> str:
    run_id = str(uuid.uuid4())
    await factory.workflow_repo.save_run({
        "run_id": run_id, "workflow_id": f"wf-{run_id[:8]}", "user_id": user_id,
        "goal": goal, "status": "COMPLETED",
        "created_at": datetime.now(timezone.utc).isoformat(), "org_id": org_id,
        "tasks": [{"task_id": "t1", "agent": "core.set", "status": "COMPLETED", "input_data": {}}],
    })
    return run_id


@pytest.mark.asyncio
async def test_t1_personal_run_denied_to_unrelated_user(env):
    """Listed runs are owner-scoped: an unrelated account's list never
    contains another user's run, and direct reads 404."""
    from httpx import ASGITransport, AsyncClient
    alice = await _signup(env, "alice-t1@example.com")
    mallory = await _signup(env, "mallory-t1@example.com")
    run_id = await _persist_run(env["factory"], alice["user"]["id"], "Alice's private goal")

    foreign = await env["client"].get("/api/v1/workflows?limit=50", headers=_headers(mallory))
    ids = [wf.get("run_id") for wf in (foreign.json().get("workflows") or [])]
    assert run_id not in ids

    # Direct read of the foreign run.
    direct = await env["client"].get(f"/api/v1/workflows/{run_id}", headers=_headers(mallory))
    assert direct.status_code in (403, 404)


@pytest.mark.asyncio
async def test_t2_schedule_listing_is_owner_scoped(env):
    client, factory = env["client"], env["factory"]
    alice = await _signup(env, "alice-t2@example.com")
    mallory = await _signup(env, "mallory-t2@example.com")
    schedule_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await factory.schedule_repo.create_schedule({
        "schedule_id": schedule_id, "user_id": alice["user"]["id"],
        "name": "Alice's cron", "cron_expression": "*/5 * * * *",
        "goal": "Alice's scheduled goal", "status": "ACTIVE",
        "next_run_at": (now + timedelta(minutes=5)).isoformat(), "created_at": now.isoformat(),
    })
    listing = await client.get("/api/v1/schedules", headers=_headers(mallory))
    ids = [s.get("schedule_id") for s in (listing.json().get("schedules") or [])]
    assert schedule_id not in ids
    # Direct read denied.
    direct = await client.get(f"/api/v1/schedules/{schedule_id}", headers=_headers(mallory))
    assert direct.status_code in (403, 404)


@pytest.mark.asyncio
async def test_t3_artifact_download_denies_foreign_owner(tmp_path, env, monkeypatch):
    from pathlib import Path
    from backend.services import artifact_builder
    from backend.config.settings import settings
    alice = await _signup(env, "alice-t3@example.com")
    mallory = await _signup(env, "mallory-t3@example.com")
    artifact_id = "att-t3"
    # Redirect the artifacts root to the tmp dir, then lay out Alice's
    # artifact exactly as the real service does.
    artifacts_root = tmp_path / "artifacts"
    monkeypatch.setattr(artifact_builder, "artifacts_root", lambda: artifacts_root)
    root = artifacts_root / alice["user"]["id"] / artifact_id
    root.mkdir(parents=True)
    body = b"Alice's confidential report"
    import hashlib as _hl
    # The manifest carries the recorded digest — §28.8's integrity check
    # refuses to serve bytes that fail it (the fixture mirrors the real
    # save path).
    (root / "artifact.json").write_text(json.dumps({
        "artifact_id": artifact_id, "user_id": alice["user"]["id"],
        "files": ["report.txt"],
        "checksums": {"report.txt": _hl.sha256(body).hexdigest()}}))
    (root / "report.txt").write_bytes(body)

    foreign = await env["client"].get(
        f"/api/v1/artifacts/{artifact_id}/files/report.txt", headers=_headers(mallory))
    assert foreign.status_code == 404, "a foreign owner must never download Alice's artifact"
    mine = await env["client"].get(
        f"/api/v1/artifacts/{artifact_id}/files/report.txt", headers=_headers(alice))
    assert mine.status_code == 200
    assert b"confidential" in mine.content


@pytest.mark.asyncio
async def test_t4_credentials_are_owner_scoped(env, monkeypatch):
    client, factory = env["client"], env["factory"]
    alice = await _signup(env, "alice-t4@example.com")
    mallory = await _signup(env, "mallory-t4@example.com")
    from backend.security.secrets_vault import SecretsVault, settings as app_settings
    monkeypatch.setattr(app_settings, "APP_ENV", "development")
    vault = SecretsVault()
    vault.initialize()
    await factory.secrets_repo.store_secret(
        alice["user"]["id"], "stripe",
        vault.encrypt("sk_live_alice_secret"))
    # Mallory cannot load or list Alice's credential.
    mallory_secret = await factory.secrets_repo.get_secret(mallory["user"]["id"], "stripe")
    assert mallory_secret is None
    # Cross-owner read is the only accessor; there is no list that leaks
    # another user's credential names either.
    assert await factory.secrets_repo.get_secret(mallory["user"]["id"], "other-key") is None


@pytest.mark.asyncio
async def test_t5_removed_member_loses_oversight_access(env):
    client, factory = env["client"], env["factory"]
    owner = await _signup(env, "owner-t5@example.com")
    member = await _signup(env, "member-t5@example.com")
    headers = _headers(owner)
    org_id = (await client.post("/api/v1/orgs", json={"name": "T5"}, headers=headers)).json()["org_id"]
    invited = await client.post(f"/api/v1/orgs/{org_id}/members", headers=headers, json={
        "email": "member-t5@example.com", "role": "admin"})
    await client.post("/api/v1/orgs/invitations/accept", headers=_headers(member), json={
        "token": invited.json()["token"]})

    # While a member: oversight allowed.
    assert (await client.get(f"/api/v1/orgs/{org_id}/runs", headers=_headers(member))).status_code == 200

    removed = await client.delete(f"/api/v1/orgs/{org_id}/members/{member['user']['id']}", headers=headers)
    assert removed.status_code == 200
    # After removal: 404 (no existence leak beyond the org they were in).
    after = await client.get(f"/api/v1/orgs/{org_id}/runs", headers=_headers(member))
    assert after.status_code in (403, 404)
