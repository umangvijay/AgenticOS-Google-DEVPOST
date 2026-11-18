"""Cancellation must retain supervision until runtime cleanup is observed."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from backend.runners.execution_store import ExecutionStore
from backend.services.isolated_runner import ExecutionRequest
from backend.services.runner_bridge import ContainerIsolatedRunner, reconcile_runner_once


@pytest.mark.asyncio
async def test_waiter_cancellation_waits_for_supervisor_cleanup(monkeypatch, tmp_path):
    started = threading.Event()
    class Runtime:
        def __init__(self, **kwargs):
            self.store = kwargs["record_store"]
        def run(self, execution_id, *args, **kwargs):
            started.set()
            import time
            deadline = time.monotonic() + 3
            while not self.store.cancellation_requested(execution_id):
                if time.monotonic() > deadline:
                    raise AssertionError("caller cancellation never reached runtime")
                time.sleep(.01)
            self.store.put(execution_id, {"exit_code": 137})
            return SimpleNamespace(cancelled=True, timed_out=False, exit_code=137,
                                   cleanup_state="cleaned", error="cancelled")
    monkeypatch.setattr("backend.services.runner_bridge.ContainerRunner", Runtime)
    runner = ContainerIsolatedRunner(image="sha256:" + "a" * 64,
                                    profile_digest="test", record_store=ExecutionStore(tmp_path / "journal"))
    request = ExecutionRequest("exec", "tenant", "artifact", runner.image, "pure_compute", [])
    await runner.submit_execution(request)
    task = asyncio.create_task(runner.run_to_completion("exec", "sleep 5", {}))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 4)
    result = await runner.get_execution("exec")
    assert result.state.value == "cancelled"
    assert result.cleanup_state.value == "cleaned"


@pytest.mark.asyncio
async def test_periodic_reconciliation_keeps_failed_cleanup_retryable(monkeypatch, tmp_path):
    class Runtime:
        def __init__(self, **kwargs):
            self.fail = True
        def remove_orphans(self, execution_id):
            if self.fail:
                raise RuntimeError("runtime unreachable")
    monkeypatch.setattr("backend.services.runner_bridge.ContainerRunner", Runtime)
    store = ExecutionStore(tmp_path / "journal")
    runner = ContainerIsolatedRunner(image="sha256:" + "a" * 64,
                                    profile_digest="test", record_store=store)
    monkeypatch.setattr("backend.services.isolated_runner._runner", runner)
    store.admit("expired", {}, 0)
    with pytest.raises(RuntimeError):
        await reconcile_runner_once()
    assert store.get("expired")["cleanup"] == "pending"
    runner.container.fail = False
    assert await reconcile_runner_once() == 1
    assert store.get("expired")["cleanup"] == "cleaned"
    assert await reconcile_runner_once() == 0
