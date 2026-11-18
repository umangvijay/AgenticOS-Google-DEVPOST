"""Real PostgreSQL parity for the contract consumed by WebAgent."""
import asyncio
import uuid

import pytest
import pytest_asyncio

from backend.models.browser_session import (
    Actor, BrowserSession, CleanupState, ControllerState, ResourceState,
    SessionTransitionError,
)


@pytest_asyncio.fixture
async def repo():
    from backend.tests.test_phase24_feedback_lifecycle_pg import _pg_up
    from backend.repositories.postgres.database import PostgresDatabaseManager
    from backend.repositories.postgres.repositories import PostgresBrowserSessionRepository
    if not _pg_up():
        pytest.skip("real PostgreSQL required on local integration port 55432")
    db = PostgresDatabaseManager("postgresql://agentos:agentos@127.0.0.1:55432/agentos_test",
                                schema="p26_browser_" + uuid.uuid4().hex)
    await db.initialize()
    try:
        yield PostgresBrowserSessionRepository(db)
    finally:
        await db.close(drop_schema=True)


def session():
    return BrowserSession("session", "tenant", "user", run_id="run", task_id="task",
                          allowed_origins=["https://example.com"])


@pytest.mark.asyncio
async def test_typed_lifecycle_checkpoint_and_legacy_registry(repo):
    created = await repo.create_session(session())
    assert isinstance(created, BrowserSession)
    ready = await repo.transition(created.session_id, ResourceState.READY, Actor.AGENT)
    assert ready.resource_state is ResourceState.READY
    with pytest.raises(SessionTransitionError):
        await repo.transition(created.session_id, ResourceState.CLOSED, Actor.AGENT)
    with pytest.raises(TypeError):
        await repo.transition(created.session_id, "CLOSED")
    checkpoint = await repo.record_checkpoint("session", checkpoint_digest="new", milestone="done", captured_at=200)
    await repo.record_checkpoint("session", checkpoint_digest="old", milestone="old", captured_at=100)
    assert (await repo.latest_checkpoint("session"))["reference"] == checkpoint
    await repo.mark_cleanup("session", CleanupState.COMPLETE)
    await repo.close_session("session")
    assert (await repo.get_session("session")).cleanup_state is CleanupState.COMPLETE
    assert await repo.get_session_for_run("run", "task") is None
    await repo.register_worker_session(session_id="viewer", worker_id="owner", endpoint="http://worker:8080",
        owner_user_id="user", run_id="run", task_id="task", controller_epoch="epoch")
    assert (await repo.get_session("viewer"))["runtime_session_id"] == "http://worker:8080"
    await repo.close_session("viewer")
    assert (await repo.get_session("viewer"))["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_contested_ownership_fences_wrong_tenant_and_stale_epoch(repo):
    await repo.create_session(session())
    assert await repo.acquire_ownership("session", "stranger", "wrong", 60, 1) is None
    results = await asyncio.gather(*(repo.acquire_ownership("session", f"w{i}", "tenant", 60, 1)
                                     for i in range(8)))
    winners = [s for s in results if s]
    assert len(winners) == 1
    owner = winners[0]
    assert not await repo.renew_ownership("session", owner.owner_worker_id, "stale", 60)
    assert await repo.renew_ownership("session", owner.owner_worker_id, owner.owner_epoch, 60)
    assert not await repo.set_controller("session", ControllerState.HUMAN, expected_owner_epoch="stale")
    assert await repo.set_controller("session", ControllerState.HUMAN, expected_owner_epoch=owner.owner_epoch)
    renewed = await repo.acquire_ownership("session", owner.owner_worker_id, "tenant", 60, 1)
    assert renewed.owner_generation > owner.owner_generation
    assert renewed.owner_epoch != owner.owner_epoch
    assert not await repo.renew_ownership("session", owner.owner_worker_id, owner.owner_epoch, 60)


@pytest.mark.asyncio
async def test_expiry_activity_and_cleanup(repo):
    s = session()
    s.idle_expires_at = 100
    s.absolute_expires_at = 300
    await repo.create_session(s)
    assert [s.session_id for s in await repo.expired_sessions(now=200)] == ["session"]
    await repo.record_activity("session", extend_idle_seconds=60, extend_absolute=True)
    assert (await repo.get_session("session")).absolute_expires_at == 300
    assert await repo.expired_sessions(now=200) == []
    assert len(await repo.expired_sessions(now=400)) == 1
    await repo.close_session("session")
    assert await repo.expired_sessions(now=400) == []


@pytest.mark.asyncio
async def test_cleanup_acknowledgement_is_bound_to_owner(repo):
    await repo.create_session(session())
    owner = await repo.acquire_ownership("session", "worker", "tenant", 60, 1)
    assert not await repo.acknowledge_cleanup("session", "other", owner.owner_epoch, complete=True)
    assert not await repo.acknowledge_cleanup("session", "worker", "stale", complete=True)
    assert await repo.acknowledge_cleanup("session", "worker", owner.owner_epoch, complete=False)
    assert (await repo.get_session("session")).resource_state is not ResourceState.CLOSED
    assert await repo.acknowledge_cleanup("session", "worker", owner.owner_epoch, complete=True)
    closed = await repo.get_session("session")
    assert closed.resource_state is ResourceState.CLOSED
    assert closed.cleanup_state is CleanupState.COMPLETE
