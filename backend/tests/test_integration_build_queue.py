"""Phase 4 §4.12: the MCP build persists as a durable worker task via ONE
atomic command acceptance — command + build record + run + task + outbox
commit together."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend.api.routers.integrations import _start_build


@pytest_asyncio.fixture
async def env(tmp_path):
    from backend.repositories.command_repository import SQLiteCommandRepository
    from backend.repositories.in_memory_message_bus import InMemoryMessageBus
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.mcp_repository import SQLiteMCPRepository
    from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
    from backend.services.command_service import set_command_repo
    db = DatabaseManager(str(tmp_path / "builds.db"))
    await db.initialize()
    factory = SimpleNamespace(
        mcp_repo=SQLiteMCPRepository(db),
        workflow_repo=SQLiteWorkflowRepository(db),
        message_bus=InMemoryMessageBus(),
        db_manager=db)
    set_command_repo(SQLiteCommandRepository(db))
    engine = SimpleNamespace(evaluate_dag=AsyncMock())
    yield SimpleNamespace(factory=factory, engine=engine, db=db)
    set_command_repo(None)
    await db.close()


@pytest.mark.asyncio
async def test_mcp_build_is_persisted_as_worker_task(env):
    result = await _start_build(env.factory, "user-1", "prompt",
                                "Build a public weather API connector",
                                "Weather", env.engine)

    assert result["status"] == "queued"
    assert result["run_id"].startswith("mcp-")
    assert result["command_id"]
    # The build record, run, and task are DURABLE (committed with the
    # command), not stubbed calls.
    build = await env.factory.mcp_repo.get_build(result["build_id"])
    assert build is not None and build["status"] == "queued"
    run = await env.factory.workflow_repo.get_run(result["run_id"])
    assert run is not None
    task = run["tasks"][0]
    assert task["agent"] == "core.mcp_build"
    assert task["input_data"]["build_id"] == result["build_id"]
    assert task["input_data"]["intent"] == "Build a public weather API connector"
    env.engine.evaluate_dag.assert_awaited_once_with(result["run_id"])
    # §4.8: the outbox event is the durable dispatch intent.
    cursor = await env.db.execute(
        "SELECT COUNT(*) FROM outbox_events WHERE command_id=?",
        (result["command_id"],))
    assert (await cursor.fetchone())[0] == 1
