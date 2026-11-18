"""Phase 27 §27.6 — J23: the declared supported-app install journey.

GET /integrations/apps/catalog returns DECLARED entries (never a claim
of execution proof) → POST /integrations/apps/{app_id}/install starts
ONE durable build for the caller → the real worker builds and validates
the adapter against the app's tested operations → activation → a
permitted agent executes a QUALIFIED operation → a documented-but-
untested operation stays unavailable → the install is idempotent →
the displayed state derives from build/validation records.
"""
import asyncio
import json
import uuid
from contextlib import suppress
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web


@pytest_asyncio.fixture
async def stack(tmp_path, monkeypatch):
    from backend.config.settings import settings
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "j23.db"))
    monkeypatch.setattr(settings, "MESSAGE_BUS_TRANSPORT", "inprocess")
    from backend.repositories.factory import RepositoryFactory
    from backend.engine.engine import WorkflowEngine
    from backend.mcp.tool_router import ToolRouter
    from backend.mcp.tool_policy import ToolPolicy
    from backend.mcp.builder.openapi_parser import OpenAPIParser
    # The sanctioned loopback-testing switch (same as the connector
    # journey): the fixture app's controlled API is a local loopback.
    monkeypatch.setattr(OpenAPIParser, "_LOOPBACK_TESTING", True)
    f = RepositoryFactory()
    await f.initialize()
    router = ToolRouter(f.mcp_repo, ToolPolicy(), secrets_repo=f.secrets_repo)
    engine = WorkflowEngine(f.workflow_repo, f.message_bus,
                            agent_factory=SimpleNamespace(tool_router=router),
                            settings_repo=f.settings_repo)
    monkeypatch.setattr("backend.worker._worker_factory", f)

    # The controlled API the fixture app's spec points at: a fresh value
    # per request so the external verifier knows the value was REAL.
    hits = {"count": 0}

    async def entries(request):
        hits["count"] += 1
        return web.json_response({"entries": [
            {"id": uuid.uuid4().hex, "amount": 37 + hits["count"]}]})

    async def total(request):
        return web.json_response({"total": sum(37 + i for i in
                                               range(1, hits["count"] + 1))})
    app = web.Application()
    app.router.add_get("/entries", entries)
    app.router.add_get("/total", total)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base_url = "http://127.0.0.1:" + str(
        site._server.sockets[0].getsockname()[1])

    # Declare the fixture app against THIS run's controlled API.
    from backend.services import app_catalog
    app_catalog.DECLARED_APPS.append({
        "app_id": "ledger-fixture",
        "version": "1.0.0",
        "name": "Ledger Fixture",
        "publisher": "agentos-qualification",
        "auth_method": "none",
        "documented_operations": ["list_entries", "ledger_total"],
        "tested_operations": ["list_entries"],
        "qualification": "fixture",
        "source": {"kind": "spec_template", "spec_format": "json",
                   "base_url_param": "base_url",
                   "spec": {"openapi": "3.0.0",
                            "info": {"title": "Ledger Fixture",
                                     "version": "1.0.0"},
                            "servers": [{"url": "{base_url}"}],
                            "paths": {
                                "/entries": {"get": {
                                    "operationId": "list_entries",
                                    "responses": {"200": {"description": "ok"}}}},
                                "/total": {"get": {
                                    "operationId": "ledger_total",
                                    "responses": {"200": {"description": "ok"}}}}}}},
        "description": "Per-test fixture app bound to a loopback API."},
    )

    from backend.worker import start_worker
    worker = asyncio.create_task(start_worker(f.message_bus, engine,
                                              user_repo=f.user_repo,
                                              mcp_repo=f.mcp_repo))
    try:
        yield {"f": f, "engine": engine, "base_url": base_url,
               "hits": hits, "router": router}
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        app_catalog.DECLARED_APPS[:] = [
            a for a in app_catalog.DECLARED_APPS
            if a["app_id"] != "ledger-fixture"]
        await runner.cleanup()
        await f.shutdown()


async def _await_build(f, build_id: str, timeout_s: float = 180.0) -> dict:
    """§29 note: the budget reflects the operation's real completion
    window under FULL-SUITE load — the journey asserts the build
    completes through the real worker, and 60 s was a suite-load
    timeout, not a product bound (attempt-2 evidence)."""
    async with asyncio.timeout(timeout_s):
        while True:
            build = await f.mcp_repo.get_build(build_id)
            if build and build.get("status") in ("success", "error"):
                return build
            await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_j23_declared_app_install_to_qualified_operation(stack):
    f = stack["f"]
    from backend.api.routers.integrations import (
        declared_app_catalog, install_declared_app, InstallAppRequest)
    from backend.api.dependencies.auth import AuthenticatedUser

    def as_user(uid: str):
        return SimpleNamespace(user_id=uid, email=f"{uid}@example.test",
                               name=uid, role="user", auth_provider="local",
                               is_admin=lambda: False)

    uid = "installer-" + uuid.uuid4().hex[:8]
    await f.user_repo.create_user({"id": uid, "email": uid + "@example.test",
                                   "name": "Installer"})

    import types
    dummy_request = types.SimpleNamespace(app=types.SimpleNamespace(
        state=types.SimpleNamespace(factory=f)))

    # 1. The catalog is DECLARED metadata — no execution claims.
    cat = await declared_app_catalog(dummy_request, as_user(uid))
    fixture = next(a for a in cat["apps"] if a["app_id"] == "ledger-fixture")
    assert fixture["documented_operations"] == ["list_entries", "ledger_total"]
    assert fixture["tested_operations"] == ["list_entries"]

    # 2. Install starts ONE durable build for the caller.
    body = InstallAppRequest(base_url=stack["base_url"])
    started = await install_declared_app("ledger-fixture", dummy_request,
                                         body, as_user(uid))
    build_id = started["build_id"]
    assert build_id and started["tested_operations"] == ["list_entries"]

    # 3. The build is idempotent: a repeated install is REFUSED (409)
    #    carrying the ORIGINAL command identity — one logical build per
    #    user+app, never a second one.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as denied:
        await install_declared_app("ledger-fixture", dummy_request,
                                   body, as_user(uid))
    assert denied.value.status_code == 409
    assert denied.value.detail.get("command_id"), \
        "the original command is retained in the denial"

    build = await _await_build(f, build_id)
    assert build["status"] == "success", build
    mcp_id = build["mcp_id"]
    manifest = await f.mcp_repo.get_mcp(mcp_id)
    assert manifest["active_revision_id"], "the qualified revision activated"

    # 4. A permitted agent executes the QUALIFIED operation through the
    #    ordinary dispatch boundary.
    router = stack["router"]
    result = await router.execute_tool(
        mcp_id + "__list_entries", {},
        context={"user_id": uid, "organization_id": ""})
    body_out = result if isinstance(result, dict) else {"raw": str(result)}
    blob = json.dumps(body_out, default=str)
    assert "entries" in blob, body_out

    # 5. The documented-but-UNTESTED operation stays unavailable — the
    #    install path gained it no validation evidence.
    from backend.mcp.tool_router import ToolRouterError
    with pytest.raises(ToolRouterError):
        await router.execute_tool(
            mcp_id + "__ledger_total", {},
            context={"user_id": uid, "organization_id": ""})

    # 6. A different user cannot invoke this user's installed tools
    #    (permission is re-checked at invocation).
    other_uid = "other-" + uuid.uuid4().hex[:8]
    await f.user_repo.create_user({"id": other_uid,
                                   "email": other_uid + "@example.test",
                                   "name": "Other"})
    with pytest.raises(ToolRouterError):
        await router.execute_tool(
            mcp_id + "__list_entries", {},
            context={"user_id": other_uid, "organization_id": ""})

    # §27.1: emit the journey's OBSERVED product receipt for the
    # qualification evidence chain (real IDs, external values, denials).
    from backend.tests.journey_observations import emit
    emit("J23", {
        "installer": uid,
        "build_id": build_id,
        "mcp_id": mcp_id,
        "active_revision_id": manifest["active_revision_id"],
        "tested_operation": "list_entries",
        "denied_untested_operation": "ledger_total",
        "duplicate_install_command_id":
            denied.value.detail.get("command_id"),
        "external_observation": body_out,
        "foreign_user": other_uid,
        "foreign_user_refused": True,
        "observed_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
    })
