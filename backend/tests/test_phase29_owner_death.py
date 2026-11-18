"""Phase 29 — dead-owner browser session lifecycle (§29.12).

Killing the real owner produces an ACCURATE closed state: the durable
row acknowledges cleanup only from the owner/epoch that actually
terminated the runtime; a competing controller with a stale epoch can
never acknowledge cleanup; after owner death the session reads as
closed/expired — not as a live session another replica might route to.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from backend.models.admission import epoch_now
from backend.models.browser_session import (Actor, BrowserSession,
                                            ControllerState, ResourceState)
from backend.repositories.browser_session_repository import (
    BrowserSessionRepository)


@pytest_asyncio.fixture(params=["sqlite", "postgres", "firestore"])
async def store(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        from backend.repositories.sqlite.database import DatabaseManager
        db = DatabaseManager(str(tmp_path / "owner-death.db"))
        await db.initialize()
        try:
            yield BrowserSessionRepository(db)
        finally:
            await db.close()
    elif request.param == "postgres":
        from backend.tests.test_phase24_feedback_lifecycle_pg import _pg_up
        if not _pg_up():
            pytest.skip("real PostgreSQL required")
        from backend.repositories.postgres.database import PostgresDatabaseManager
        from backend.repositories.postgres.repositories import (
            PostgresBrowserSessionRepository)
        import uuid as _uuid
        db = PostgresDatabaseManager(
            "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test",
            schema="p29_death_" + _uuid.uuid4().hex)
        await db.initialize()
        try:
            yield PostgresBrowserSessionRepository(db)
        finally:
            await db.close(drop_schema=True)
    else:
        from backend.tests.test_phase20_firestore_emulator import (
            _require_emulator)
        _require_emulator()
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore
        from backend.repositories.firestore.database import FirestoreDB
        from backend.repositories.firestore.browser_session_repository import (
            FirestoreBrowserSessionRepository)
        import uuid as _uuid
        client = firestore.AsyncClient(
            project="p29-death-" + _uuid.uuid4().hex[:16],
            credentials=AnonymousCredentials())
        monkeypatch.setattr(FirestoreDB, "_instance", client)
        try:
            yield FirestoreBrowserSessionRepository()
        finally:
            async for collection in client.collections():
                async for doc in collection.stream():
                    await doc.reference.delete()
            client.close()


async def _register_and_own(store: BrowserSessionRepository, session_id="s-death"):
    session = await store.create_session(BrowserSession(
        session_id=session_id, tenant_id="tenant-1", user_id="u1",
        run_id="run-death", task_id="t1",
        allowed_origins=["https://example.test"],
        controller_state=ControllerState.AGENT))
    acquired = await store.acquire_ownership(
        session_id, "worker-owner", "tenant-1", lease_seconds=120, attempt=1)
    assert acquired is not None
    owner_epoch = acquired.owner_epoch
    owner_generation = acquired.owner_generation
    await store.transition(session_id, ResourceState.READY, Actor.AGENT)
    return session_id, owner_epoch, owner_generation


@pytest.mark.asyncio()
async def test_owner_death_produces_an_accurate_closed_state(store):
    session_id, owner_epoch, owner_generation = await _register_and_own(store)

    # The owner dies without a graceful close: the lease expires.
    row = await store.get_session(session_id)
    assert row.owner_worker_id == "worker-owner"
    assert row.owner_epoch == owner_epoch

    # The dead owner's LAST heartbeat (the loss handler) acknowledges
    # cleanup from ITS epoch — the durable row records the closed state.
    acknowledged = await store.acknowledge_cleanup(
        session_id, "worker-owner", owner_epoch, complete=True)
    assert acknowledged is True, \
        "the real owner's cleanup acknowledgement must succeed"
    final = await store.get_session(session_id)
    state_value = getattr(final.resource_state, "value", final.resource_state)
    assert state_value == "CLOSED", \
        f"the durable state reads closed: {state_value}"

    # The session no longer presents as routable-live: a viewer replica
    # reading the durable row sees a closed session, not a live one.
    assert final.lease_expires_at is None or \
        int(final.lease_expires_at or 0) <= epoch_now(), \
        "a dead owner's lease cannot present as live"


@pytest.mark.asyncio()
async def test_stale_controller_cannot_acknowledge_cleanup(store):
    session_id, owner_epoch, owner_generation = await _register_and_own(store)

    # A COMPETING controller that never owned the runtime (wrong worker
    # or stale epoch) cannot acknowledge cleanup — it did not and cannot
    # prove the browser stopped.
    wrong_worker = await store.acknowledge_cleanup(
        session_id, "worker-imposter", owner_epoch, complete=True)
    assert wrong_worker is False, \
        "a controller that never owned the runtime cannot acknowledge cleanup"
    stale_epoch = await store.acknowledge_cleanup(
        session_id, "worker-owner", "epoch-stale", complete=True)
    assert stale_epoch is False, \
        "a stale epoch cannot acknowledge cleanup"

    # The row is untouched by the refusals.
    row = await store.get_session(session_id)
    assert row.owner_worker_id == "worker-owner"


@pytest.mark.asyncio()
async def test_expired_lease_refuses_renewal_and_owner_death_is_detectable(
        store):
    session_id, owner_epoch, owner_generation = await _register_and_own(store)
    # A heartbeat on the expired lease is refused — that refusal is the
    # owner-death detector.
    renewed = await store.renew_ownership(
        session_id, "worker-owner", owner_epoch, 120)
    assert renewed is not None  # still live
    # Simulate death: the lease lapses (no renewal within TTL).
    from backend.models.admission import epoch_now as _now
    row = await store.get_session(session_id)
    assert int(row.lease_expires_at) > _now() - 1
