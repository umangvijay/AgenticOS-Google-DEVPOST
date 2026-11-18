"""Phase 28 §28.4 — the worker-interruption drills for the self-building
MCP journey on PostgreSQL.

The retained Phase 26/27 suites prove the uninterrupted journey (real
worker builds a standalone stdio server, protocol validation, activation,
parent resume, tamper refusal) on PostgreSQL and Firestore, plus
restart + exactly-one-wake on Firestore and NATS replay. This file adds
the named close-when drills: the worker dies DURING generation and
AGAIN between validation and activation, and a replacement worker
completes the SAME logical build — one effective activation, one
logical parent continuation, verified fresh value in the parent output.
"""
import asyncio
import json
import uuid
from contextlib import suppress

import pytest

from backend.tests.test_phase26_connector_journey import stack  # noqa: F401
from backend.tests.test_phase26_standalone_journey import (  # noqa: F401
    managed_runner, specification,
)


def parent_with_wait(parent_id, uid, values):
    return {"run_id": parent_id, "user_id": uid,
            "goal": "Total actual ledger values", "status": "RUNNING",
            "tasks": [{"task_id": "calculate", "status": "WAITING",
                       "agent": "core.connector_call",
                       "input_data": {"operation": "ledger_total",
                                      "arguments": {"entries": values}},
                       "max_retries": 0, "recovery_enabled": False}]}


async def _poll_parent(f, parent_id, child_run_id, *, timeout, interval=0.1):
    """Poll until the parent is COMPLETED; fail honestly if either run
    vanishes or the child hard-fails."""
    import time as _time
    deadline = _time.monotonic() + timeout
    parent = child = None
    while _time.monotonic() < deadline:
        parent = await f.workflow_repo.get_run(parent_id)
        child = await f.workflow_repo.get_run(child_run_id)
        assert parent is not None and child is not None, (
            f"run vanished: parent={bool(parent)} child={bool(child)}")
        if parent["status"] == "COMPLETED":
            return parent
        assert child["status"] != "FAILED", child
        await asyncio.sleep(interval)
    raise AssertionError(
        f"poll incomplete after {timeout}s: parent={parent['status']} "
        f"child={child['status']} tasks="
        f"{[(t['task_id'], t['status'], t.get('attempt'), t.get('lease_owner'), t.get('error')) for t in child.get('tasks', [])]}")


def _start_worker(f, engine):
    from backend.worker import start_worker
    return asyncio.create_task(start_worker(
        f.message_bus, engine, user_repo=f.user_repo, org_repo=f.org_repo,
        mcp_repo=f.mcp_repo))


async def _stop(task):
    if task and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", ["postgres"], indirect=True)
async def test_worker_killed_during_generation_completes_the_build(
        stack, managed_runner):
    """Kill the worker as soon as the child build task starts executing
    (during generation); the replacement worker finishes the SAME build
    and the parent resumes exactly once."""
    f, engine, uid, _, _, _, router = stack
    values = [int(uuid.uuid4().hex[:6], 16), -7]
    parent_id = "restart-parent-" + uuid.uuid4().hex
    await f.workflow_repo.save_run(parent_with_wait(parent_id, uid, values))
    from backend.services.connector_build_service import (
        ORIGIN_ORCHESTRATOR, request_durable_build)
    tracking = await request_durable_build(
        f, user_id=uid, method="python_tools",
        source=json.dumps(specification()), name="Interrupted ledger MCP",
        origin=ORIGIN_ORCHESTRATOR, parent_run_id=parent_id,
        parent_task_id="calculate", required_operations=["ledger_total"])
    worker = _start_worker(f, engine)
    try:
        # Wait until the child build task is actually RUNNING, then kill
        # the worker mid-flight (the bus nacks the in-flight delivery).
        async with asyncio.timeout(20):
            while True:
                child = await f.workflow_repo.get_run(tracking["run_id"])
                if child and child["status"] == "RUNNING":
                    break
                assert child is not None and child["status"] != "FAILED", child
                await asyncio.sleep(0.05)
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        # The replacement worker recovers the nacked delivery and finishes
        # the build from the durable state.
        replacement = _start_worker(f, engine)
        try:
            parent = await _poll_parent(f, parent_id, tracking["run_id"],
                                        timeout=60)
            output = parent["tasks"][0]["output_data"]
            if isinstance(output, str):
                output = json.loads(output)
            assert output["structuredContent"] == {"total": sum(values)}
            build = await f.mcp_repo.get_build(tracking["build_id"])
            manifest = await f.mcp_repo.get_mcp(build["mcp_id"])
            assert manifest["active_revision_id"], manifest
            assert manifest["artifact_type"] == "standalone_server"
            wait = await f.wait_repo.latest_wait(parent_id, "calculate")
            assert wait["state"] == "resolved"
        finally:
            await _stop(replacement)
    finally:
        await _stop(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", ["postgres"], indirect=True)
async def test_worker_killed_between_validation_and_activation_recovers(
        stack, managed_runner, monkeypatch):
    """The worker dies while protocol validation is in flight, before any
    activation; the replacement worker re-executes the build task, and
    exactly ONE revision ends up active."""
    f, engine, uid, _, _, _, router = stack
    from backend.services import standalone_build

    values = [int(uuid.uuid4().hex[:6], 16), 13]
    parent_id = "restart-parent-" + uuid.uuid4().hex
    await f.workflow_repo.save_run(parent_with_wait(parent_id, uid, values))
    from backend.services.connector_build_service import (
        ORIGIN_ORCHESTRATOR, request_durable_build)
    tracking = await request_durable_build(
        f, user_id=uid, method="python_tools",
        source=json.dumps(specification()), name="Mid-validation MCP",
        origin=ORIGIN_ORCHESTRATOR, parent_run_id=parent_id,
        parent_task_id="calculate", required_operations=["ledger_total"])

    release = asyncio.Event()
    validation_entries = []

    real_validate = standalone_build.validate_artifact

    async def blocking_validate(*args, **kwargs):
        validation_entries.append(uuid.uuid4().hex)
        if len(validation_entries) == 1:
            # First attempt: the worker "dies" here, before activation.
            await release.wait()
            raise asyncio.CancelledError
        return await real_validate(*args, **kwargs)

    monkeypatch.setattr(standalone_build, "validate_artifact",
                        blocking_validate)

    worker = _start_worker(f, engine)
    try:
        # Wait until the first validation attempt is in flight, then kill
        # the worker while it holds the build task.
        async with asyncio.timeout(40):
            while not validation_entries:
                child = await f.workflow_repo.get_run(tracking["run_id"])
                assert child is not None and child["status"] != "FAILED", child
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)  # let the container launch under validation
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        # A worker that died mid-task leaves the task lease-fenced until
        # the TTL expires; the in-memory bus dead-letters bounded nacks.
        # Simulate the TTL elapsing, then REDLIVER the message through the
        # normal trigger path — the replacement worker re-claims (attempt 2)
        # and completes the build.
        from datetime import datetime, timedelta, timezone
        await f.workflow_repo.update_task(tracking["run_id"], "mcp-build",
            {"lease_expires_at": (datetime.now(timezone.utc)
                                  - timedelta(seconds=1)).isoformat()})
        release.set()
        from backend.models.schemas import TaskTriggerEvent
        child = await f.workflow_repo.get_run(tracking["run_id"])
        await f.message_bus.publish("agentos-workflow-events", TaskTriggerEvent(
            workflow_id=child["workflow_id"], run_id=child["run_id"],
            task_id="mcp-build"))
        replacement = _start_worker(f, engine)
        try:
            parent = await _poll_parent(f, parent_id, tracking["run_id"],
                                        timeout=60)
            output = parent["tasks"][0]["output_data"]
            if isinstance(output, str):
                output = json.loads(output)
            assert output["structuredContent"] == {"total": sum(values)}
            build = await f.mcp_repo.get_build(tracking["build_id"])
            manifest = await f.mcp_repo.get_mcp(build["mcp_id"])
            assert manifest["active_revision_id"], manifest
            # Exactly one effective activation for this build: one stored
            # revision, matching the active revision id.
            revisions = await f.mcp_repo.list_revisions(build["mcp_id"])
            assert len(revisions) == 1, revisions
            assert revisions[0]["revision_id"] == manifest["active_revision_id"]
            wait = await f.wait_repo.latest_wait(parent_id, "calculate")
            assert wait["state"] == "resolved"
        finally:
            await _stop(replacement)
    finally:
        await _stop(worker)
