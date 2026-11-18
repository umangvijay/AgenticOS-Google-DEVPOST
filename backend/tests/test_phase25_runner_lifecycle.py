"""Exercise cancellation, independent outputs and durable admission on real containers."""
import asyncio
import subprocess
import uuid

import pytest

from backend.runners.execution_store import ExecutionStore
from backend.services.isolated_runner import ExecutionRequest, RunnerUnavailableError
from backend.services.runner_bridge import ContainerIsolatedRunner


@pytest.mark.asyncio
async def test_two_executions_cancel_and_restart(tmp_path):
    inspect = subprocess.run(["docker", "image", "inspect", "agentos-runner:latest",
                              "--format", "{{.Id}}"], capture_output=True, text=True)
    if inspect.returncode:
        pytest.skip("qualification requires the local runner image and Docker daemon")
    image = inspect.stdout.strip()
    path = tmp_path / "journal.db"
    def worker():
        return ContainerIsolatedRunner(image=image, profile_digest="test-profile",
                                       record_store=ExecutionStore(path))
    runner = worker()
    other = worker()
    def request():
        return ExecutionRequest(uuid.uuid4().hex, "tenant", "sha256:test", image,
                                "pure_compute", ["compute"], deadline_seconds=12)
    slow, fast = request(), request()
    await runner.submit_execution(slow)
    await runner.submit_execution(fast)
    with pytest.raises(RunnerUnavailableError):
        await other.submit_execution(slow)
    task = asyncio.create_task(runner.run_to_completion(slow.execution_id, "sleep 10", {}))
    try:
        result = await runner.run_to_completion(fast.execution_id, "echo output-two", {})
        assert result.state.value == "completed", (
            result.exit_reason, await runner.collect_outputs(fast.execution_id))
        assert await other.request_cancellation(slow.execution_id)
        assert (await task).state.value == "cancelled"
        assert (await other.collect_outputs(fast.execution_id))[0]["stdout_tail"].strip() == "output-two"
        assert (await other.get_execution(slow.execution_id)).cleanup_state.value == "cleaned"
        assert await other.request_cancellation(slow.execution_id)
    finally:
        # Drain the actual supervisor before pytest closes this event loop,
        # including when the independent fast execution fails to launch.
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runner.shutdown()
        await other.shutdown()


@pytest.mark.asyncio
async def test_cancelled_coroutine_stops_real_container(tmp_path):
    inspect = subprocess.run(["docker", "image", "inspect", "agentos-runner:latest",
                              "--format", "{{.Id}}"], capture_output=True, text=True)
    if inspect.returncode:
        pytest.skip("qualification requires Docker and the local runner image")
    runner = ContainerIsolatedRunner(image=inspect.stdout.strip(), profile_digest="test-profile",
                                    record_store=ExecutionStore(tmp_path / "journal.db"))
    request = ExecutionRequest(uuid.uuid4().hex, "tenant", "sha256:test", runner.image,
                               "pure_compute", ["compute"], deadline_seconds=20)
    await runner.submit_execution(request)
    task = asyncio.create_task(runner.run_to_completion(request.execution_id, "sleep 15", {}))
    try:
        for _ in range(100):
            record = await runner.get_execution(request.execution_id)
            if record.sandbox_identity:
                break
            await asyncio.sleep(.05)
        assert record.sandbox_identity, "real container was never launched"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        record = await runner.get_execution(request.execution_id)
        assert record.state.value == "cancelled"
        assert record.cleanup_state.value == "cleaned"
        containers = await asyncio.to_thread(runner.container._client.containers.list,
            all=True, filters={"label": f"agentos.execution={request.execution_id}"})
        assert containers == []
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runner.shutdown()
