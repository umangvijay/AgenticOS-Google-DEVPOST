"""Regression: child completion alone must not bypass capability validation."""
import json
import uuid

import pytest
import pytest_asyncio

from backend.config.settings import settings

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pg_stack(monkeypatch, tmp_path):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 55432), timeout=2):
            pass
    except OSError:
        pytest.skip("BLOCKED: no Postgres on :55432 — docker run ... "
                    "postgres:16-alpine (§21.2)")
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "postgres")
    monkeypatch.setattr(settings, "POSTGRES_DSN",
                        "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test")
    monkeypatch.setattr(settings, "MESSAGE_BUS_TRANSPORT", "inprocess")
    from backend.repositories.factory import RepositoryFactory
    from backend.repositories.postgres.database import PostgresDatabaseManager
    db = PostgresDatabaseManager(settings.POSTGRES_DSN, schema="p26_wait_" + uuid.uuid4().hex)
    monkeypatch.setattr("backend.repositories.postgres.database.PostgresDatabaseManager",
                          lambda dsn, **kwargs: db)
    factory = RepositoryFactory()
    await factory.initialize()
    from backend.engine.engine import WorkflowEngine
    from backend.repositories.in_memory_message_bus import InMemoryMessageBus
    engine = WorkflowEngine(factory.workflow_repo, InMemoryMessageBus())
    yield {"factory": factory, "engine": engine}
    await db.close(drop_schema=True)
    await factory.shutdown()


async def test_completed_child_without_validated_capability_keeps_parent_parked(pg_stack):
    """Manually setting completion cannot grant a built capability."""
    f = pg_stack["factory"]
    uid = f"flagship-{uuid.uuid4().hex[:8]}"  # unique admission identity
    run_id = f"parent-{uuid.uuid4().hex[:10]}"
    # 1. The parent run with a WAITING task (the orchestrator parked it
    #    when it detected the missing capability).
    await f.workflow_repo.save_run({
        "run_id": run_id, "user_id": uid,
        "goal": "Read the ledger through the new tool",
        "status": "RUNNING",
        "tasks": [{"task_id": "read-ledger", "run_id": run_id,
                   "status": "WAITING",
                   "agent": "core.http",
                   "input_data": {"url": "https://127.0.0.1:9/entries"},
                   "title": "read ledger"}],
    })
    await f.workflow_repo.update_task(run_id, "read-ledger",
                                      {"status": "WAITING"})

    # 2. The orchestrator requests the missing capability: the DURABLE
    #    build creates the child run + task + the parent_wait in the
    #    acceptance transaction (§12.8 production path).
    from backend.services.connector_build_service import (
        ORIGIN_ORCHESTRATOR, request_durable_build)
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Ledger API", "version": "1.0.0"},
        "servers": [{"url": "http://127.0.0.1:9"}],
        "paths": {"/entries": {"get": {
            "operationId": "listEntries",
            "responses": {"200": {"description": "ok"}}}}},
    }
    track = await request_durable_build(
        f, user_id=uid, method="spec", source=json.dumps(spec),
        name="Ledger", origin=ORIGIN_ORCHESTRATOR,
        parent_run_id=run_id, parent_task_id="read-ledger")
    build_id = track["build_id"]
    child_run_id = track.get("run_id")
    record = await f.mcp_repo.get_build(build_id)
    assert record["parent_run_id"] == run_id, \
        "the waiting parent is recoverable from the build record"

    # The wait exists (open, waiting on this child).
    wait = await f.wait_repo.find_open_wait(run_id, "read-ledger")
    assert wait is not None, "the parent_wait was committed with the build"
    assert wait["child_run_id"] == child_run_id

    # Simulate a premature completion flag without executing the build.
    from backend.models.schemas import Task, TaskStatus, WorkflowRun
    child_run = await f.workflow_repo.get_run(child_run_id)
    assert child_run is not None, "the child run was committed"
    from backend.engine.repo_adapter import dict_to_run
    child_obj = dict_to_run(child_run)
    for task in child_obj.tasks:
        await f.workflow_repo.update_task(child_run_id, task.task_id,
                                          {"status": "COMPLETED"})
    await f.workflow_repo.update_run_status(child_run_id, "COMPLETED")

    # A terminal child flag alone is not activation evidence. This fixture
    # did not execute the factory; it must stay parked. The real positive
    # journey is in test_phase26_connector_journey.py.
    from backend.services.command_reconciler import _resolve_parent_waits
    resolved = await _resolve_parent_waits(
        f.wait_repo, f.workflow_repo, pg_stack["engine"].message_bus,
        batch=25, mcp_repo=f.mcp_repo)
    assert resolved == 0
    assert await f.wait_repo.find_open_wait(run_id, "read-ledger")
    await pg_stack["engine"].evaluate_dag(run_id)
    parent = await f.workflow_repo.get_run(run_id)
    assert parent["tasks"][0]["status"] == "WAITING"
