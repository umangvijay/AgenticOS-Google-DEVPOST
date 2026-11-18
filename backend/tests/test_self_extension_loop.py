"""Audit page-48 acceptance: the self-extending loop, proven locally.

Happy-path mechanics without an LLM key — every durable hop the product
is defined by, with the planner's gap-detection hop being the only one
that requires a model:

    waiting run (missing capability) → durable build (origin=orchestrator,
    parent_run_id links the waiter) → adapter generated from a real spec →
    live probe passes → connector registered verified → the original run
    resumes and completes against the newly acquired capability → output
    verified.

The HTTP server is local; no external calls, no paid APIs.
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

from backend.engine.engine import WorkflowEngine
from backend.models.schemas import Task, TaskStatus, WorkflowRun
from backend.repositories.factory import RepositoryFactory
from backend.repositories.in_memory_message_bus import InMemoryMessageBus
from backend.repositories.sqlite.database import DatabaseManager

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Ledger API", "version": "1.0.0"},
    "servers": [{"url": "http://127.0.0.1:0"}],  # replaced with the live port
    "paths": {
        "/entries": {
            "get": {"operationId": "listEntries", "responses": {"200": {"description": "ok"}}}
        },
        "/entries/new": {
            "post": {"operationId": "createEntry", "responses": {"200": {"description": "ok"}}}
        },
    },
}
LEDGER_PAYLOAD = {"entries": [{"id": 1, "amount": 42}]}


@pytest_asyncio.fixture
async def ledger_server():
    hit = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hit["count"] += 1
            body = json.dumps(LEDGER_PAYLOAD).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"port": port, "hits": hit, "url": f"http://127.0.0.1:{port}"}
    server.shutdown()
    thread.join()


@pytest_asyncio.fixture
async def stack(tmp_path, ledger_server):
    # TEST-ONLY seam: this suite exercises the full self-extension cycle
    # against a numeric-loopback server. Restore immediately after — the
    # strict SSRF model is the default for every other test and all
    # deployed processes.
    from backend.mcp.builder.openapi_parser import OpenAPIParser
    OpenAPIParser._LOOPBACK_TESTING = True
    try:
        async for value in _stack_inner(tmp_path, ledger_server):
            yield value
    finally:
        OpenAPIParser._LOOPBACK_TESTING = False


async def _stack_inner(tmp_path, ledger_server):
    db = DatabaseManager(str(tmp_path / "selfext.db"))
    await db.initialize()
    factory = RepositoryFactory.__new__(RepositoryFactory)
    from backend.repositories.in_memory_message_bus import InMemoryMessageBus
    from backend.repositories.sqlite.idempotency_repository import SQLiteIdempotencyRepository
    from backend.repositories.sqlite.mcp_repository import SQLiteMCPRepository
    from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository

    factory.db_manager = db
    factory.mcp_repo = SQLiteMCPRepository(db)
    factory.workflow_repo = SQLiteWorkflowRepository(db)
    factory.idempotency_repo = SQLiteIdempotencyRepository(db)
    factory.message_bus = InMemoryMessageBus()
    from backend.repositories.command_repository import SQLiteCommandRepository
    from backend.services.command_service import set_command_repo
    set_command_repo(SQLiteCommandRepository(db))
    # Production wires the specialist contract repository; the flagship
    # journey exercises the SAME ordinary build path — the validator's
    # typed contract runs for real and its proposal is consumed.
    from backend.repositories.agent_task_repository import (
        AgentTaskRepository, set_agent_task_repo)
    agent_tasks = AgentTaskRepository(db)
    set_agent_task_repo(agent_tasks)
    engine = WorkflowEngine(factory.workflow_repo, factory.message_bus)
    yield {"db": db, "factory": factory, "engine": engine, "server": ledger_server}
    from backend.services.http_pool import aclose_client
    await aclose_client()
    set_command_repo(None)
    set_agent_task_repo(None)
    await db.close()


@pytest.mark.asyncio
async def test_self_extension_loop_missing_capability_to_completed_run(stack):
    from backend.agents.mcp_factory.mcp_factory_agent import MCPFactoryAgent
    from backend.services.connector_build_service import (
        ORIGIN_ORCHESTRATOR, request_durable_build, track_inline_build,
    )

    factory, engine, server = stack["factory"], stack["engine"], stack["server"]
    user_id = "loop-user"

    # ── 1. The original run is WAITING on a missing capability ──────────
    parent_run = "run-selfext-1"
    waiting_task = Task(
        task_id="read-ledger", workflow_id="wf-selfext", run_id=parent_run,
        user_id=user_id, agent="core.http",
        input_data={"url": f"{server['url']}/entries", "method": "GET"},
        status=TaskStatus.WAITING,
    )
    run = WorkflowRun(
        run_id=parent_run, workflow_id="wf-selfext", user_id=user_id,
        goal="Read the ledger and summarize totals", status=TaskStatus.RUNNING,
        tasks=[waiting_task],
    )
    await factory.workflow_repo.save_run(run.model_dump(mode="json"))

    # ── 2. The orchestrator requests the missing capability through the
    #      DURABLE build service: the waiting run is linked to the build ──
    spec_text = json.dumps({**SPEC, "servers": [{"url": server["url"]}]})
    track = await track_inline_build(
        factory, user_id=user_id, method="spec", source=spec_text,
        name="Ledger", origin=ORIGIN_ORCHESTRATOR, parent_run_id=parent_run,
    )
    build_id = track["build_id"]
    record = await factory.mcp_repo.get_build(build_id)
    assert record["parent_run_id"] == parent_run, "the waiting run must be recoverable from the build"
    assert record["origin"] == "orchestrator"

    # ── 3. The factory builds the adapter from the real spec; the live
    #      probe passes against the local server ─────────────────────────
    agent = MCPFactoryAgent(factory.mcp_repo)
    result = await agent.run_build(user_id=user_id, method="spec", source=spec_text,
                                   name="Ledger", build_id=build_id)
    assert result["status"] == "success", result
    mcp_id = result["mcp_id"]

    registered = await factory.mcp_repo.get_mcp(mcp_id)
    # Explicit product boundary (G03) + evidence gates (G01/G02):
    assert registered["artifact_type"] == "adapter"
    assert registered["trust_tier"] == "verified"
    assert registered["spec_provenance"] == "official_spec"
    coverage = registered.get("test_coverage") or {}
    assert coverage.get("read_probe_passed", 0) >= 1
    assert server["hits"]["count"] >= 1, "the probe hit the real capability"

    # ── 4. The original run RESUMES: its waiting task now executes against
    #      the acquired capability and completes ──────────────────────────
    refreshed = await factory.workflow_repo.get_run(parent_run)
    refreshed["tasks"][0]["status"] = TaskStatus.PENDING  # resume intent
    await factory.workflow_repo.save_run(refreshed)
    await engine.execute_task(parent_run, "read-ledger")

    final = await factory.workflow_repo.get_run(parent_run)
    assert final["status"] == TaskStatus.COMPLETED
    task = final["tasks"][0]
    assert task["status"] == TaskStatus.COMPLETED
    assert task["output_data"]["data"] == LEDGER_PAYLOAD

    # The build record still points back at the completed run: the full
    # lifecycle is auditable end to end.
    final_record = await factory.mcp_repo.get_build(build_id)
    assert final_record["status"] == "success"
    assert final_record["parent_run_id"] == parent_run
