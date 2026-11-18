"""Contested browser control must have one winner on every supported store."""
import asyncio
import time
import uuid

import pytest
import pytest_asyncio

from backend.models.browser_session import BrowserSession, ControllerState, ResourceState


@pytest_asyncio.fixture(params=["sqlite", "postgres", "firestore"])
async def store(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        from backend.repositories.sqlite.database import DatabaseManager
        from backend.repositories.browser_session_repository import BrowserSessionRepository
        db = DatabaseManager(str(tmp_path / "control.db"))
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
        from backend.repositories.postgres.repositories import PostgresBrowserSessionRepository
        db = PostgresDatabaseManager("postgresql://agentos:agentos@127.0.0.1:55432/agentos_test",
                                     schema="p26_control_" + uuid.uuid4().hex)
        await db.initialize()
        try:
            yield PostgresBrowserSessionRepository(db)
        finally:
            await db.close(drop_schema=True)
    else:
        from backend.tests.test_phase20_firestore_emulator import _require_emulator
        _require_emulator()
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore
        from backend.repositories.firestore.database import FirestoreDB
        from backend.repositories.firestore.browser_session_repository import FirestoreBrowserSessionRepository
        client = firestore.AsyncClient(project="p26-control-" + uuid.uuid4().hex[:16], credentials=AnonymousCredentials())
        monkeypatch.setattr(FirestoreDB, "_instance", client)
        try:
            yield FirestoreBrowserSessionRepository()
        finally:
            async for collection in client.collections():
                async for doc in collection.stream():
                    await doc.reference.delete()
            client.close()


@pytest.mark.asyncio
async def test_control_cas_fences_stale_generations_workers_and_expired_leases(store):
    await store.create_session(BrowserSession("session", "tenant", "owner",
        owner_worker_id="worker", owner_epoch="epoch", owner_generation=1,
        lease_expires_at=int(time.time()) + 60, resource_state=ResourceState.READY,
        controller_state=ControllerState.AGENT))
    assert not await store.change_runtime_control("session", "wrong", "epoch", 0, ControllerState.HUMAN)
    assert not await store.change_runtime_control("session", "worker", "stale", 0, ControllerState.HUMAN)
    results = await asyncio.gather(*(store.change_runtime_control(
        "session", "worker", "epoch", 0, ControllerState.HUMAN) for _ in range(6)))
    assert sum(result is not None for result in results) == 1
    current = await store.get_session("session")
    assert current.controller_generation == 1
    assert current.controller_state is ControllerState.HUMAN
    assert not await store.change_runtime_control("session", "worker", "epoch", 0, ControllerState.AGENT)
    returned = await store.change_runtime_control("session", "worker", "epoch", 1, ControllerState.AGENT)
    assert returned.controller_generation == 2
    await store.close_session("session")
    assert not await store.change_runtime_control("session", "worker", "epoch", 2, ControllerState.HUMAN)
    await store.create_session(BrowserSession("expired", "tenant", "owner",
        owner_worker_id="worker", owner_epoch="epoch", lease_expires_at=1,
        resource_state=ResourceState.READY))
    assert not await store.change_runtime_control("expired", "worker", "epoch", 0, ControllerState.AGENT)
