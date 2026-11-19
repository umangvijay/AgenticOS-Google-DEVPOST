"""Build and resume against real PostgreSQL, HTTP and the production worker.

No generated tool, probe result, validation receipt or completion is mocked.
The loopback allowance is limited to this isolated integration fixture.
"""
import asyncio
from contextlib import suppress
import json
import os
from types import SimpleNamespace
import uuid

import pytest
import pytest_asyncio
from aiohttp import web


@pytest_asyncio.fixture
async def stack(monkeypatch, request):
    from backend.tests.test_phase24_feedback_lifecycle_pg import _pg_up
    backend = getattr(request, "param", "postgres")
    if backend == "postgres" and not _pg_up():
        pytest.skip("real PostgreSQL required on :55432")
    from backend.config.settings import settings
    from backend.repositories.postgres.database import PostgresDatabaseManager
    from backend.repositories.factory import RepositoryFactory
    from backend.mcp.builder.openapi_parser import OpenAPIParser
    db = client = None
    if backend == "postgres":
        db = PostgresDatabaseManager("postgresql://agentos:agentos@127.0.0.1:55432/agentos_test",
                                     schema="p26_connector_" + uuid.uuid4().hex)
        monkeypatch.setattr("backend.repositories.postgres.database.PostgresDatabaseManager",
                          lambda dsn, **kwargs: db)
    else:
        from backend.tests.test_phase20_firestore_emulator import _require_emulator
        from backend.repositories.firestore.database import FirestoreDB
        from backend.repositories.in_memory_message_bus import InMemoryMessageBus
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore
        _require_emulator()
        client = firestore.AsyncClient(project="p26-build-" + uuid.uuid4().hex[:16], credentials=AnonymousCredentials())
        monkeypatch.setattr(settings, "GOOGLE_CLOUD_PROJECT", client.project)
        monkeypatch.setattr(FirestoreDB, "_instance", client)
        monkeypatch.setattr("backend.repositories.pubsub_message_bus.PubSubMessageBus", InMemoryMessageBus)
    monkeypatch.setattr(settings, "STORAGE_BACKEND", backend)
    monkeypatch.setattr(settings, "MESSAGE_BUS_TRANSPORT", "inprocess")
    monkeypatch.setattr(OpenAPIParser, "_LOOPBACK_TESTING", True)
    f = RepositoryFactory()
    await f.initialize()
    uid = "owner-" + uuid.uuid4().hex
    await f.user_repo.create_user({"id": uid, "email": uid + "@example.test", "name": "Fixture owner"})
    payload = {"entries": [{"id": uuid.uuid4().hex, "amount": 37}]}
    hits = []
    async def entries(request):
        hits.append(request.path)
        return web.json_response(payload)
    app = web.Application()
    app.router.add_get("/entries", entries)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    from backend.mcp.tool_router import ToolRouter
    from backend.mcp.tool_policy import ToolPolicy
    from backend.engine.engine import WorkflowEngine
    router = ToolRouter(f.mcp_repo, ToolPolicy(), secrets_repo=f.secrets_repo)
    engine = WorkflowEngine(f.workflow_repo, f.message_bus,
                            agent_factory=SimpleNamespace(tool_router=router), settings_repo=f.settings_repo)
    monkeypatch.setattr("backend.worker._worker_factory", f)
    try:
        yield f, engine, uid, url, payload, hits, router
    finally:
        from backend.services.http_pool import aclose_client
        await aclose_client()
        await runner.cleanup()
        if db is not None:
            await db.close(drop_schema=True)
        if client is not None:
            # This is the private, randomly named emulator project created above.
            # The emulator clears nested documents synchronously without the SDK
            # bulk-writer retry race during teardown.
            import httpx
            host = os.environ["FIRESTORE_EMULATOR_HOST"]
            async with httpx.AsyncClient(trust_env=False) as admin:
                response = await admin.delete(
                    f"http://{host}/emulator/v1/projects/{client.project}/databases/(default)/documents")
                response.raise_for_status()
            client.close()
        await f.shutdown()
        from backend.repositories.wait_repository import set_wait_repo
        from backend.services.command_service import set_command_repo
        set_wait_repo(None)
        set_command_repo(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", ["postgres", "firestore"], indirect=True)
async def test_generated_adapter_activates_and_resumes_parent_without_manual_flips(stack):
    f, engine, uid, url, payload, hits, router = stack
    parent_id = "parent-" + uuid.uuid4().hex
    await f.workflow_repo.save_run({"run_id": parent_id, "user_id": uid,
        "goal": "Read dynamically generated ledger", "status": "RUNNING", "tasks": [{
            "task_id": "read", "run_id": parent_id, "status": "WAITING",
            "agent": "core.connector_call", "input_data": {"operation": "listEntries", "arguments": {}},
            "title": "Read entries", "max_retries": 0}]})
    await f.workflow_repo.update_task(parent_id, "read", {"status": "WAITING"})
    spec = {"openapi": "3.0.0", "info": {"title": "Ledger", "version": "1.0"},
        "servers": [{"url": url}], "paths": {"/entries": {"get": {
            "operationId": "listEntries", "responses": {"200": {"description": "ok"}}}}}}
    from backend.services.connector_build_service import request_durable_build, ORIGIN_ORCHESTRATOR
    track = await request_durable_build(f, user_id=uid, method="spec", source=json.dumps(spec),
        name="Ledger", origin=ORIGIN_ORCHESTRATOR, parent_run_id=parent_id,
        parent_task_id="read", required_operations=["listEntries"])
    build = await f.mcp_repo.get_build(track["build_id"])
    assert build["required_operations"] == ["listEntries"]
    child = await f.workflow_repo.get_run(track["run_id"])
    assert len(child["tasks"]) == 1, "acceptance must persist the actual executable build task"
    await engine.evaluate_dag(parent_id)
    assert (await f.workflow_repo.get_run(parent_id))["tasks"][0]["status"] == "WAITING"

    from backend.worker import start_worker
    worker = asyncio.create_task(start_worker(f.message_bus, engine, user_repo=f.user_repo,
        org_repo=f.org_repo, mcp_repo=f.mcp_repo))
    try:
        async with asyncio.timeout(25):
            while True:
                parent = await f.workflow_repo.get_run(parent_id)
                child = await f.workflow_repo.get_run(track["run_id"])
                assert child["status"] != "FAILED", child
                assert parent["status"] != "FAILED", parent
                if parent["status"] == "COMPLETED":
                    break
                await asyncio.sleep(.1)
        assert child["status"] == "COMPLETED"
        assert child["tasks"][0]["attempt"] == 1
        assert payload["entries"][0]["id"] in json.dumps(parent["tasks"][0]["output_data"])
        assert len(hits) >= 2, "both factory probe and parent invocation must reach the real API"
        build = await f.mcp_repo.get_build(track["build_id"])
        connector = await f.mcp_repo.get_mcp(build["mcp_id"])
        assert connector["active_revision_id"]
        validation = await f.mcp_repo.list_validation_runs(build["mcp_id"])
        assert any(v["status"] == "passed" for v in validation)
        wait = await f.wait_repo.latest_wait(parent_id, "read")
        assert wait["state"] == "resolved"
        if f.db_manager is None:
            from backend.repositories.firestore.database import FirestoreDB
            db = await FirestoreDB.get_client()
            wake = (await db.collection("outbox_events").document("wake-" + wait["wait_id"]).get()).to_dict()
        else:
            wake = await f.db_manager.fetch_one("SELECT * FROM pg_outbox_v2 WHERE event_id=$1", "wake-" + wait["wait_id"])
        assert wake["event_type"] == "parent.ready"
        # Mutation after activation must not reuse the previous probe's
        # authority; the parent invocation path rechecks actual tool bytes.
        from backend.services.built_capability import invoke_built_capability
        ctx = {"user_id": uid, "run_id": parent_id, "task_id": "read"}
        tools = await f.mcp_repo.get_cached_tools(build["mcp_id"])
        assert tools[0]["operation"]["path"] == "/entries"
        tools[0]["operation"]["path"] = "/changed-after-validation"
        await f.mcp_repo.cache_tools(build["mcp_id"], tools)
        previous_hits = len(hits)
        with pytest.raises(Exception, match="VALIDATION|validat|eligib|policy|permitted|allow"):
            await invoke_built_capability(router, f.workflow_repo, ctx,
                                         {"operation": "listEntries"})
        assert len(hits) == previous_hits
        await f.mcp_repo.set_mcp_enabled(build["mcp_id"], False)
        with pytest.raises(RuntimeError, match="disabled"):
            await invoke_built_capability(router, f.workflow_repo, ctx, {"operation": "listEntries"})
        with pytest.raises(PermissionError):
            await f.mcp_repo.register_mcp({"mcp_id": build["mcp_id"], "owner": "stranger", "name": "replaced"})
        assert (await f.mcp_repo.get_mcp(build["mcp_id"]))["owner"] == uid
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_db_discovered_task_honors_revoked_owner_before_it_can_call_a_tool(stack):
    f, engine, uid, url, _, hits, _ = stack
    run_id = "denied-" + uuid.uuid4().hex
    await f.workflow_repo.save_run({"run_id": run_id, "user_id": uid, "goal": "Read entries",
        "status": "RUNNING", "tasks": [{"task_id": "read", "status": "PENDING",
            "agent": "core.http", "input_data": {"url": url + "/entries"}}]})
    await f.user_repo.delete_user(uid)
    # No bus event or command is published. Only the database recovery loop
    # discovers this task, so the bus listener cannot mask a missing check.
    from backend.worker import start_worker
    worker = asyncio.create_task(start_worker(f.message_bus, engine, user_repo=f.user_repo,
                                              org_repo=f.org_repo, mcp_repo=f.mcp_repo))
    try:
        async with asyncio.timeout(10):
            while (run := await f.workflow_repo.get_run(run_id))["status"] != "FAILED":
                await asyncio.sleep(.05)
        assert "owner_missing" in run["tasks"][0]["error"]
        assert run["tasks"][0]["attempt"] == 0
        assert hits == []
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", ["postgres", "firestore"], indirect=True)
async def test_failed_factory_build_durably_blocks_waiting_parent(stack):
    f, engine, uid, _, _, hits, _ = stack
    parent_id = "failed-parent-" + uuid.uuid4().hex
    await f.workflow_repo.save_run({"run_id": parent_id, "user_id": uid,
        "goal": "Use a missing capability", "status": "RUNNING", "tasks": [{
            "task_id": "read", "status": "WAITING", "agent": "core.connector_call",
                "input_data": {"operation": "listEntries"}, "max_retries": 0, "attempt": 0}]})
    from backend.services.connector_build_service import request_durable_build, ORIGIN_ORCHESTRATOR
    track = await request_durable_build(f, user_id=uid, method="spec", source="{}",
        name="Invalid API definition", origin=ORIGIN_ORCHESTRATOR,
        parent_run_id=parent_id, parent_task_id="read", required_operations=["listEntries"])
    child = await f.workflow_repo.get_run(track["run_id"])
    # Set execution policy before starting the real worker. The invalid
    # definition must fail through the factory, without artificial status flips.
    await f.workflow_repo.update_task(track["run_id"], child["tasks"][0]["task_id"],
        {"max_retries": 0, "recovery_enabled": False})
    from backend.worker import start_worker
    worker = asyncio.create_task(start_worker(f.message_bus, engine, user_repo=f.user_repo,
                                             org_repo=f.org_repo, mcp_repo=f.mcp_repo))
    try:
        async with asyncio.timeout(25):
            while (parent := await f.workflow_repo.get_run(parent_id))["status"] != "FAILED":
                await asyncio.sleep(.1)
        child = await f.workflow_repo.get_run(track["run_id"])
        assert child["status"] == "FAILED"
        task = parent["tasks"][0]
        assert task["status"] == "BLOCKED"
        assert "Required capability build failed" in task["error"]
        assert task["attempt"] == 0
        wait = await f.wait_repo.latest_wait(parent_id, "read")
        assert wait["state"] == "failed"
        assert not await f.wait_repo.resolve_with_wake(wait, {}, outcome="failed")
        assert hits == []
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
