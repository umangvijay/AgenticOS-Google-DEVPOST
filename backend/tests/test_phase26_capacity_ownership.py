"""Cancellation and stale cleanup must not leak or transfer local permits."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.models.admission import CapacityExhaustedError, ResourceClass
from backend.services import capacity_service as capacity

RC = ResourceClass.WORKFLOW_TASK


@pytest.fixture(autouse=True)
def isolated_capacity(monkeypatch):
    monkeypatch.setattr(capacity, "_local_semaphores", {})
    monkeypatch.setattr(capacity, "_LOCALLY_ACQUIRED", type(capacity._LOCALLY_ACQUIRED)())
    monkeypatch.setattr(capacity, "METRICS", capacity._Metrics())
    monkeypatch.setattr(capacity, "_local_limit", lambda _: 2)
    monkeypatch.setattr(capacity, "get_capacity_repo", lambda: None)


async def take(scope, key):
    return await capacity.acquire(RC, scope, scope, key)


@pytest.mark.asyncio
async def test_cancelled_shared_admission_returns_local_slot(monkeypatch):
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(capacity, "_local_limit", lambda _: 1)
    monkeypatch.setattr(capacity, "get_capacity_repo", lambda: SimpleNamespace(acquire=blocked))
    pending = asyncio.create_task(take("tenant-a", "first"))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    monkeypatch.setattr(capacity, "get_capacity_repo", lambda: None)
    lease = await take("tenant-a", "first")
    await capacity.release(lease)
    assert capacity.capacity_metrics()["local_active_workflow_task"] == 0


@pytest.mark.asyncio
async def test_same_execution_key_in_different_tenants_releases_both_slots():
    first = await take("tenant-a", "same-key")
    second = await take("tenant-b", "same-key")
    await capacity.release(first)
    await capacity.release(second)
    fresh = [await take("tenant-c", str(i)) for i in range(2)]
    for lease in fresh:
        await capacity.release(lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, "worker", "generation"])
async def test_stale_or_foreign_release_cannot_free_replacement(foreign, monkeypatch):
    monkeypatch.setattr(capacity, "_local_limit", lambda _: 1)
    old = await take("tenant", "same-key")
    await capacity.release(old)
    current = await take("tenant", "same-key")
    stale = (replace(current, worker_id="other-worker") if foreign == "worker" else
             replace(current, generation=current.generation + 1) if foreign == "generation" else old)
    await capacity.release(stale)
    try:
        with pytest.raises(CapacityExhaustedError):
            await take("tenant", "must-defer")
    finally:
        await capacity.release(current)


@pytest.mark.asyncio
async def test_duplicate_local_execution_defers_without_allocating_twice():
    first = await take("tenant", "work")
    try:
        with pytest.raises(CapacityExhaustedError):
            await take("tenant", "work")
        second = await take("tenant", "other-work")
        await capacity.release(second)
    finally:
        await capacity.release(first)


@pytest.mark.asyncio
async def test_release_by_key_cannot_release_another_process_lease(monkeypatch):
    called = []
    async def foreign_release(*args):
        called.append(args)
        return True
    monkeypatch.setattr(capacity, "get_capacity_repo", lambda: SimpleNamespace(release_execution=foreign_release))
    assert not await capacity.release_execution(RC, "tenant", "foreign-execution")
    assert called == []


@pytest.mark.asyncio
async def test_duplicate_pending_admission_does_not_call_store_twice(monkeypatch):
    entered = asyncio.Event()
    calls = []
    async def blocked(*args, **kwargs):
        calls.append(args)
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(capacity, "get_capacity_repo", lambda: SimpleNamespace(acquire=blocked))
    first = asyncio.create_task(take("tenant", "same"))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        with pytest.raises(CapacityExhaustedError):
            await asyncio.wait_for(take("tenant", "same"), 1)
        assert len(calls) == 1
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_other_live_loop_cannot_replace_process_guard(monkeypatch):
    monkeypatch.setattr(capacity, "_local_limit", lambda _: 1)
    held = await take("tenant", "first")
    def other_worker():
        async def run():
            with pytest.raises(CapacityExhaustedError):
                await take("tenant", "second")
        asyncio.run(run())
    try:
        await asyncio.to_thread(other_worker)
        with pytest.raises(CapacityExhaustedError):
            await take("tenant", "third")
    finally:
        await capacity.release(held)


def test_dead_loop_cleanup_never_releases_replacement_guard(monkeypatch):
    monkeypatch.setattr(capacity, "_local_limit", lambda _: 1)
    old = asyncio.run(take("tenant", "same"))
    async def replacement():
        new = await take("tenant", "same")
        await capacity.release(old)
        with pytest.raises(CapacityExhaustedError):
            await take("tenant", "different")
        await capacity.release(new)
        assert capacity.capacity_metrics()["local_active_workflow_task"] == 0
    asyncio.run(replacement())


@pytest.mark.asyncio
async def test_release_by_key_uses_original_shared_fence(monkeypatch, tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.capacity_repository import SQLiteCapacityRepository
    db = DatabaseManager(str(tmp_path / "capacity.db"))
    await db.initialize()
    repo = SQLiteCapacityRepository(db)
    monkeypatch.setattr(capacity, "get_capacity_repo", lambda: repo)
    try:
        lease = await take("tenant", "work")
        # The durable lease is now owned by a newer attempt. Closing the old
        # browser must not use unfenced release_execution to retire that row.
        await repo.release(lease.lease_id, lease.worker_id, lease.generation)
        newer = await repo.acquire(RC, "tenant", "tenant", "work", limit=2, worker_id="replacement")
        await capacity.release_execution(RC, "tenant", "work")
        assert await repo.active_units(RC, "tenant") == 1
        assert not await capacity.release_execution(RC, "tenant", "work")
        await repo.release(newer.lease.lease_id, "replacement", newer.lease.generation)
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["build", "step", "claim", "event", "command"])
async def test_engine_cancellation_before_dispatch_returns_all_capacity(monkeypatch, point):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    from backend.engine import engine as module
    from backend.repositories import wait_repository, execution_step_repository
    from backend.services import command_service
    entered = asyncio.Event()
    async def stop(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(capacity, "_local_limit", lambda _: 1)
    task = SimpleNamespace(task_id="task", status="PENDING", dependencies=[],
                           timeout_seconds=60, attempt=0, agent="core.mcp_build",
                           started_at=datetime.now(timezone.utc))
    run = SimpleNamespace(run_id="run", workflow_id="workflow", user_id="owner", status="RUNNING", tasks=[task])
    async def claim(*args):
        if point == "claim":
            await stop()
        task.attempt += 1
        return True
    engine = module.WorkflowEngine.__new__(module.WorkflowEngine)
    engine.repo = SimpleNamespace(claim_task=claim)
    engine._defer_counts = {}
    engine._emit_event_sync = stop if point == "event" else AsyncMock()
    monkeypatch.setattr(module, "load_run", AsyncMock(return_value=run))
    monkeypatch.setattr(wait_repository, "get_wait_repo", lambda: None)
    monkeypatch.setattr(execution_step_repository, "get_execution_step_repo", lambda:
        SimpleNamespace(create_step=stop, transition=AsyncMock()) if point == "step" else None)
    monkeypatch.setattr(command_service, "get_command_repo", lambda:
        SimpleNamespace(get_command_by_run=stop) if point == "command" else None)
    original = capacity.acquire
    async def admission(resource, *args, **kwargs):
        if resource == ResourceClass.MCP_BUILD and point == "build":
            await stop()
        return await original(resource, *args, **kwargs)
    monkeypatch.setattr(capacity, "acquire", admission)
    running = asyncio.create_task(engine.execute_task("run", "task"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    monkeypatch.setattr(capacity, "acquire", original)
    for resource in (RC, ResourceClass.MCP_BUILD):
        lease = await capacity.acquire(resource, "other", "other", "fresh")
        await capacity.release(lease)
