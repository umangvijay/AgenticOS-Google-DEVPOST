"""Phase 26 carryover R7: feedback through real Firestore transactions and HTTP."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

import pytest
import pytest_asyncio
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.config.settings import settings
from backend.repositories.firestore.database import FirestoreDB
from backend.repositories.firestore.feedback_repository import FirestoreFeedbackRepository
from backend.tests.test_phase20_firestore_emulator import _require_emulator


@pytest_asyncio.fixture
async def fs_client():
    _require_emulator()
    # Every test gets its own emulator-only project. No shared app rows are touched.
    client = firestore.AsyncClient(project="agentos-p26-" + uuid.uuid4().hex[:16],
                                   credentials=AnonymousCredentials())
    try:
        yield client
    finally:
        async for collection in client.collections():
            async for snapshot in collection.stream():
                await snapshot.reference.delete()
        client.close()


@pytest_asyncio.fixture
async def stack(fs_client, monkeypatch):
    from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
    from backend.api.routers.feedback import router
    from backend.repositories.factory import RepositoryFactory
    from backend.repositories.in_memory_message_bus import InMemoryMessageBus
    monkeypatch.setattr(settings, "GOOGLE_CLOUD_PROJECT", fs_client.project)
    monkeypatch.setattr(FirestoreDB, "_instance", fs_client)
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "firestore")
    # Queue transport is outside this HTTP/storage test; never reach real Pub/Sub.
    monkeypatch.setattr("backend.repositories.pubsub_message_bus.PubSubMessageBus", InMemoryMessageBus)
    factory = RepositoryFactory()
    await factory.initialize()
    assert isinstance(factory.feedback_repo, FirestoreFeedbackRepository)
    await factory.workflow_repo.save_run({"run_id": "run", "user_id": "owner",
        "tasks": [{"task_id": "task"}], "model_version": "model-26", "policy_version": "policy-26"})
    app = FastAPI(version="phase26-test")
    app.state.factory = factory
    identity = {"user_id": "owner", "role": "user"}
    async def actor():
        return AuthenticatedUser(identity["user_id"], "test@example.com", "Test", identity["role"], "local")
    app.dependency_overrides[get_current_user] = actor
    app.include_router(router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, factory.feedback_repo, identity, factory
    finally:
        await factory.shutdown()
        from backend.repositories.deliverable_repository import set_deliverable_repo
        from backend.services.command_service import set_command_repo
        from backend.services.resource_authorization import set_resource_authorization_service
        from backend.repositories.sqlite.adaptive_policy_repository import set_adaptive_policy_repo
        from backend.repositories.factory import set_active_bus
        for reset in (set_deliverable_repo, set_command_repo, set_resource_authorization_service,
                      set_adaptive_policy_repo, set_active_bus):
            reset(None)


def payload(**extra):
    return {"helpful": False, "category": "incorrect_result", "run_id": "run", **extra}


@pytest.mark.asyncio
async def test_http_consent_versions_admin_triage_and_owner_deletion(stack):
    client, repo, identity, _ = stack
    ids = []
    for consent in (False, True):
        response = await client.post("/feedback", json=payload(task_id="task", share_diagnostics=consent))
        assert response.status_code == 201, response.text
        fid = response.json()["feedback_id"]
        ids.append(fid)
        row = await repo.get(fid)
        assert (row["model_version"], row["policy_version"], row["app_version"]) == (
            "model-26", "policy-26", "phase26-test")
        assert row["share_diagnostics"] is consent
    assert (await client.get("/feedback")).status_code == 403
    assert (await client.post(f"/feedback/{ids[0]}/triage", json={"status": "fixed"})).status_code == 403
    identity["role"] = "admin"
    queue = {r["id"]: r for r in (await client.get("/feedback")).json()["feedback"]}
    assert queue[ids[0]]["run_id"] is None and queue[ids[0]]["task_id"] is None
    assert queue[ids[1]]["run_id"] == "run" and queue[ids[1]]["task_id"] == "task"
    for status in ("investigating", "planned", "fixed"):
        response = await client.post(f"/feedback/{ids[1]}/triage", json={"status": status, "note": "contract test"})
        assert response.status_code == 200
    assert (await repo.get(ids[1]))["triage_actor"] == "owner"
    assert [r["id"] for r in (await client.get("/feedback?status_filter=fixed")).json()["feedback"]] == [ids[1]]
    identity.update(user_id="stranger", role="user")
    assert (await client.get("/feedback/mine?run_id=run")).json()["feedback"] == []
    assert (await client.delete(f"/feedback/{ids[1]}")).status_code == 404
    identity["user_id"] = "owner"
    assert len((await client.get("/feedback/mine?run_id=run")).json()["feedback"]) == 2
    assert (await client.delete(f"/feedback/{ids[1]}")).status_code == 200
    assert await repo.get(ids[1]) is None


@pytest.mark.asyncio
async def test_http_reference_checks_and_admission_failure(stack):
    client, repo, identity, factory = stack
    identity["user_id"] = "stranger"
    assert (await client.post("/feedback", json=payload())).status_code == 404
    identity["user_id"] = "owner"
    assert (await client.post("/feedback", json=payload(task_id="wrong"))).status_code == 404
    assert (await client.post("/feedback", json=payload(task_id="task", run_id=None))).status_code == 400
    assert await repo.list_for_user("owner") == []
    factory.shared_rate_repo = None
    assert (await client.post("/feedback", json=payload())).status_code == 503
    assert await repo.list_for_user("owner") == []


@pytest.mark.asyncio
async def test_firestore_factory_binds_live_organization_authorization(stack):
    _, _, _, factory = stack
    from backend.services.resource_authorization import AuthorizationDenied
    org = await factory.org_repo.create_org("Phase 26 test", "owner")
    ctx = await factory.resource_authorization.require_membership(org["org_id"], "owner", "owner")
    assert ctx.role == "owner"
    with pytest.raises(AuthorizationDenied, match="not_a_member"):
        await factory.resource_authorization.require_membership(org["org_id"], "stranger", "viewer")


@pytest.mark.asyncio
async def test_two_clients_see_durable_feedback_and_delete_once(fs_client):
    first = FirestoreFeedbackRepository(fs_client)
    fid = await first.submit({"user_id": "owner", "helpful": True, "category": "other"})
    second_client = firestore.AsyncClient(project=fs_client.project, credentials=AnonymousCredentials())
    try:
        second = FirestoreFeedbackRepository(second_client)
        assert (await second.get(fid))["user_id"] == "owner"
        assert not await second.delete_user_feedback("stranger", fid)
        outcomes = await asyncio.gather(first.delete_user_feedback("owner", fid),
                                        second.delete_user_feedback("owner", fid))
        assert sorted(outcomes) == [False, True]
        assert not await first.set_triage(fid, "fixed", "admin")
        assert await second.get(fid) is None
    finally:
        second_client.close()


@pytest.mark.asyncio
async def test_triage_racing_deletion_never_recreates_feedback(fs_client):
    repo = FirestoreFeedbackRepository(fs_client)
    for _ in range(5):
        fid = await repo.submit({"user_id": "owner", "helpful": True, "category": "other"})
        _, deleted = await asyncio.gather(repo.set_triage(fid, "planned", "admin"),
                                          repo.delete_user_feedback("owner", fid))
        assert deleted
        assert await repo.get(fid) is None


@pytest.mark.asyncio
async def test_retention_paginates_and_concurrent_workers_count_actual_deletions(fs_client):
    repo = FirestoreFeedbackRepository(fs_client)
    repo.RETENTION_BATCH_SIZE = 2
    old = []
    for _ in range(5):
        fid = await repo.submit({"user_id": "owner", "helpful": True, "category": "other"})
        await fs_client.collection("feedback").document(fid).update({
            "created_at": datetime.now(timezone.utc) - timedelta(days=90)})
        old.append(fid)
    fresh = await repo.submit({"user_id": "owner", "helpful": True, "category": "other"})
    results = await asyncio.gather(repo.apply_retention(30), repo.apply_retention(30))
    assert sum(results) == 5
    for fid in old:
        assert await repo.get(fid) is None
    assert await repo.get(fresh) is not None
    assert await repo.apply_retention(30) == 0


@pytest.mark.asyncio
async def test_filtered_lists_order_limits_and_untrusted_fields(fs_client):
    repo = FirestoreFeedbackRepository(fs_client)
    ids = []
    for index in range(4):
        fid = await repo.submit({"user_id": "owner", "helpful": True, "category": "other",
            "run_id": "run-a" if index % 2 else "run-b", "comment": "a" * 5000,
            "created_at": datetime(2000, 1, 1, tzinfo=timezone.utc), "triage_status": "fixed"})
        ids.append(fid)
    assert [r["id"] for r in await repo.list_for_user("owner", limit=2)] == ids[::-1][:2]
    assert [r["id"] for r in await repo.list_for_user("owner", run_id="run-a")] == [ids[3], ids[1]]
    assert [r["id"] for r in await repo.list_for_triage(limit=2)] == ids[:2]
    assert await repo.list_for_user("owner", limit=0) == []
    row = await repo.get(ids[0])
    assert row["triage_status"] == "received" and row["created_at"].year != 2000
    assert len(row["comment"]) == 4000
    with pytest.raises(ValueError):
        await repo.set_triage(ids[0], "not-a-status", "admin")


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [-1, 0, True, 1.5])
async def test_invalid_retention_cannot_delete_feedback(fs_client, days):
    repo = FirestoreFeedbackRepository(fs_client)
    fid = await repo.submit({"user_id": "owner", "helpful": True, "category": "other"})
    with pytest.raises(ValueError):
        await repo.apply_retention(days)
    assert await repo.get(fid) is not None


def test_feedback_query_indexes_are_packaged():
    data = json.loads((Path(__file__).resolve().parents[2] / "firestore.indexes.json").read_text())
    actual = {tuple((field["fieldPath"], field["order"]) for field in index["fields"])
              for index in data["indexes"] if index["collectionGroup"] == "feedback"}
    assert actual == {
        (("user_id", "ASCENDING"), ("created_at", "DESCENDING")),
        (("user_id", "ASCENDING"), ("run_id", "ASCENDING"), ("created_at", "DESCENDING")),
        (("triage_status", "ASCENDING"), ("created_at", "ASCENDING"))}


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"helpful": "yes"}, {"category": "unknown"}, {"user_id": ""}])
async def test_invalid_submission_is_not_persisted(fs_client, overrides):
    repo = FirestoreFeedbackRepository(fs_client)
    with pytest.raises(ValueError):
        await repo.submit({"user_id": "owner", "helpful": True, "category": "other", **overrides})
    assert await repo.list_for_triage() == []


@pytest.mark.asyncio
async def test_browser_cleanup_acknowledgement_is_owner_fenced(fs_client):
    from backend.models.browser_session import BrowserSession, CleanupState, ResourceState
    from backend.repositories.firestore.browser_session_repository import FirestoreBrowserSessionRepository
    repo = FirestoreBrowserSessionRepository()
    repo._db = fs_client
    await repo.create_session(BrowserSession("browser", "owner", "owner", owner_worker_id="worker",
                                            owner_epoch="epoch", resource_state=ResourceState.READY))
    assert not await repo.acknowledge_cleanup("browser", "other", "epoch", complete=True)
    assert not await repo.acknowledge_cleanup("browser", "worker", "old", complete=True)
    assert await repo.acknowledge_cleanup("browser", "worker", "epoch", complete=False)
    failed = await repo.get_session("browser")
    assert failed.cleanup_state is CleanupState.FAILED
    assert failed.resource_state is ResourceState.READY
    assert await repo.acknowledge_cleanup("browser", "worker", "epoch", complete=True)
    closed = await repo.get_session("browser")
    assert closed.resource_state is ResourceState.CLOSED
    assert closed.cleanup_state is CleanupState.COMPLETE

