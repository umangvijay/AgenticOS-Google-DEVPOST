"""Shared PostgreSQL journal and real cancellation from another process."""
import asyncio
import subprocess
import sys
import time
import uuid

import pytest

from backend.tests.test_phase26_connector_journey import stack  # noqa: F401
from backend.tests.test_phase26_standalone_journey import managed_runner  # noqa: F401

DSN = "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test"


def assert_journal_fences_completion(owner, controller):
    deadline = time.time() + 60
    owner.admit("race", {}, deadline)
    assert owner.transition("race", ["queued"], "running")
    owner.put("race", {"exit_code": 0})
    assert controller.transition("race", ["running"], "cancel_requested")
    owner.finish("race", "completed", "cleaned", "exit:0")
    row = owner.get("race")
    assert (row["state"], row["cleanup"], row["reason"]) == (
        "cancelled", "cleaned", "cancelled before result commit")
    with pytest.raises(ValueError):
        owner.put("race", {"exit_code": 1})
    assert owner.get("race")["result"] == row["result"]

    owner.admit("already-done", {}, deadline)
    assert owner.transition("already-done", ["queued"], "running")
    owner.finish("already-done", "completed", "cleaned", "exit:0")
    assert not controller.transition("already-done", ["running"], "cancel_requested")
    assert owner.get("already-done")["state"] == "completed"

    owner.admit("before-launch", {}, deadline)
    assert controller.cancel_queued("before-launch")
    assert not owner.transition("before-launch", ["queued"], "running")
    assert owner.get("before-launch")["cleanup"] == "cleaned"


def test_sqlite_cancel_and_result_commit_order(tmp_path):
    from backend.runners.execution_store import ExecutionStore
    path = tmp_path / "journal.db"
    assert_journal_fences_completion(ExecutionStore(path), ExecutionStore(path))


@pytest.mark.asyncio
async def test_postgres_cancel_and_result_commit_order(stack):
    from backend.runners.postgres_execution_store import PostgresExecutionStore
    schema = stack[0].db_manager._schema
    owner = PostgresExecutionStore(DSN, schema=schema)
    controller = PostgresExecutionStore(DSN, schema=schema)
    try:
        owner.bind_host("runner-host")
        controller.bind_host("control-host")
        assert_journal_fences_completion(owner, controller)
    finally:
        controller.close()
        owner.close()


@pytest.mark.asyncio
@pytest.mark.timeout(110)
async def test_replacement_supervisor_reaps_real_orphan_after_process_death(stack, managed_runner):
    from backend.runners.postgres_execution_store import PostgresExecutionStore
    from backend.services.runner_bridge import ContainerIsolatedRunner
    schema = stack[0].db_manager._schema
    execution_id = "crash-" + uuid.uuid4().hex
    journal = PostgresExecutionStore(DSN, schema=schema)
    replacement = ContainerIsolatedRunner(image=managed_runner.image,
        profile_digest=managed_runner.profile_digest, record_store=journal)
    child_source = """
import asyncio, sys
from backend.runners.postgres_execution_store import PostgresExecutionStore
from backend.services.isolated_runner import ExecutionRequest
from backend.services.runner_bridge import ContainerIsolatedRunner

async def main():
    dsn, schema, image, profile, execution = sys.argv[1:]
    runner = ContainerIsolatedRunner(image=image, profile_digest=profile,
        record_store=PostgresExecutionStore(dsn, schema=schema))
    await runner.submit_execution(ExecutionRequest(execution, 'crash-fixture', 'fixture',
        image, 'pure_compute', deadline_seconds=8))
    await runner.run_to_completion(execution, 'touch /work/started && sleep 30', {})
    await runner.shutdown()

asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", child_source, DSN,
        schema, managed_runner.image, managed_runner.profile_digest, execution_id,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    filters = {"label": "agentos.execution=" + execution_id}
    containers = replacement.container._client.containers
    try:
        async with asyncio.timeout(12):
            while True:
                if child.returncode is not None:
                    detail = (await child.stderr.read()).decode(errors="replace").splitlines()
                    raise AssertionError(f"Supervisor exited {child.returncode} before crash injection: "
                                         + (detail[-1] if detail else "no diagnostic"))
                matches = await asyncio.to_thread(containers.list, filters=filters)
                if matches:
                    marker = await asyncio.to_thread(matches[0].exec_run, ["test", "-f", "/work/started"])
                    if marker.exit_code == 0:
                        break
                await asyncio.sleep(.1)
        child.kill()
        await child.wait()
        assert child.returncode < 0
        assert await asyncio.to_thread(containers.list, filters=filters)
        assert journal.get(execution_id)["state"] == "running"
        assert await replacement.reconcile_on_restart() == 0
        # Wait for the actual persisted deadline. The drill does not rewrite it.
        await asyncio.sleep(max(0, journal.get(execution_id)["deadline"] - time.time()) + .1)
        assert await replacement.reconcile_on_restart() == 1
        final = await replacement.get_execution(execution_id)
        assert final.state.value == "failed"
        assert final.cleanup_state.value == "cleaned"
        assert final.exit_reason == "expired supervisor lease"
        assert await asyncio.to_thread(containers.list, all=True, filters=filters) == []
        assert await replacement.reconcile_on_restart() == 0
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        await asyncio.to_thread(replacement.container.remove_orphans, execution_id)
        await replacement.shutdown()


@pytest.mark.asyncio
async def test_shared_journal_cancels_real_container_from_separate_process(stack, managed_runner):
    from backend.runners.postgres_execution_store import PostgresExecutionStore
    from backend.services.runner_bridge import ContainerIsolatedRunner
    from backend.services.isolated_runner import ExecutionRequest, RunnerUnavailableError
    schema = stack[0].db_manager._schema
    store = await asyncio.to_thread(PostgresExecutionStore, DSN, schema=schema)
    runner = ContainerIsolatedRunner(image=managed_runner.image, profile_digest=managed_runner.profile_digest, record_store=store)
    request = ExecutionRequest("shared-" + uuid.uuid4().hex, "tenant", "artifact", runner.image, "pure_compute", deadline_seconds=15)
    task = None
    try:
        await runner.submit_execution(request)
        with pytest.raises(RunnerUnavailableError, match="already admitted"):
            await runner.submit_execution(request)
        task = asyncio.create_task(runner.run_to_completion(request.execution_id, "sleep 12", {}))
        async with asyncio.timeout(10):
            while not (await runner.get_execution(request.execution_id)).sandbox_identity:
                await asyncio.sleep(.05)
        control = """
import sys
from backend.runners.postgres_execution_store import PostgresExecutionStore
s = PostgresExecutionStore(sys.argv[1], schema=sys.argv[2])
try:
    s.bind_host('independent-control-replica')
    assert s.get(sys.argv[3])['state'] == 'running'
    assert s.transition(sys.argv[3], ['running'], 'cancel_requested')
finally:
    s.close()
"""
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-c", control, DSN, schema, request.execution_id], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        stopped = await asyncio.wait_for(task, 8)
        assert stopped.state.value == "cancelled"
        assert stopped.cleanup_state.value == "cleaned"
        assert store.get(request.execution_id)["runner_host_id"] == runner.container._client.info()["ID"]
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runner.shutdown()


@pytest.mark.asyncio
async def test_reconciliation_cannot_close_another_hosts_execution(stack, managed_runner):
    from backend.runners.postgres_execution_store import PostgresExecutionStore
    from backend.services.runner_bridge import ContainerIsolatedRunner
    schema = stack[0].db_manager._schema
    foreign = await asyncio.to_thread(PostgresExecutionStore, DSN, schema=schema)
    local = await asyncio.to_thread(PostgresExecutionStore, DSN, schema=schema)
    foreign.bind_host("different-runtime-host")
    foreign.admit("foreign", {}, time.time() - 10)
    foreign.transition("foreign", ["queued"], "running")
    foreign.admit("queued-foreign", {}, time.time() + 100)
    runner = ContainerIsolatedRunner(image=managed_runner.image, profile_digest=managed_runner.profile_digest, record_store=local)
    try:
        assert await runner.reconcile_on_restart() == 0
        assert foreign.get("foreign")["state"] == "running"
        assert foreign.get("foreign")["cleanup"] == "pending"
        assert await runner.request_cancellation("queued-foreign")
        assert foreign.get("queued-foreign")["state"] == "cancelled"
        assert not foreign.transition("queued-foreign", ["queued"], "running")
    finally:
        await runner.shutdown()
        await asyncio.to_thread(foreign.close)
