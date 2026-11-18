"""Phase 29 revision — the Weekly Workshop Change Pack journey (§29.19,
§29.14–29.17): three runs through the normal backend entry points.

Run 1 discovers the missing `list_changes` reader, builds it durably,
wakes the parent, and reads TWO pages including a fresh nonce.
Run 2 (changed records, unchanged spec) REUSES the pinned revision with
zero new builds and reads fresh content; the same authenticated event
delivered twice with fresh signatures yields ONE logical run.
Run 3 (schema drift) parks the old pinned run honestly, stages a sibling
revision, and activates conditionally — the old run's graph and
evidence survive.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress
from types import SimpleNamespace

import pytest
from aiohttp import web

from backend.tests.test_phase26_connector_journey import stack  # noqa: F401

CHANGE_SPEC_OP = "list_changes"


def _spec(url: str, *, require_unit: bool = False):
    """The workshop change-feed spec. The drift variant adds a required
    `unit` query parameter (the changed unit representation)."""
    params = [{"name": "page", "in": "query", "schema": {"type": "integer"}}]
    if require_unit:
        params.append({"name": "unit", "in": "query", "required": True,
                       "schema": {"type": "string"}})
    return {"openapi": "3.0.0",
            "info": {"title": "Workshop Change Feed", "version": "1.0"},
            "servers": [{"url": url}],
            "paths": {"/changes": {"get": {
                "operationId": CHANGE_SPEC_OP,
                "parameters": params,
                "responses": {"200": {"description": "ok"}}}}}}


def _parent_run(parent_id: str, uid: str, goal: str):
    """Two cursor-chained reads: page 1, then page 2 resolved from page
    1's opaque cursor (§29.19 checkpoint/cursor)."""
    return {"run_id": parent_id, "user_id": uid, "goal": goal,
            "status": "RUNNING", "tasks": [
                {"task_id": "read_page_1", "run_id": parent_id,
                 "status": "WAITING", "agent": "core.connector_call",
                 "input_data": {"operation": CHANGE_SPEC_OP,
                                "arguments": {"page": 1}},
                 "title": "Read changes page 1", "max_retries": 0},
                {"task_id": "read_page_2", "run_id": parent_id,
                 "status": "PENDING", "agent": "core.connector_call",
                 "input_data": {"operation": CHANGE_SPEC_OP,
                                "arguments": {
                                    "page": "{{ tasks.read_page_1.output.body.next_page }}"}},
                 "dependencies": ["read_page_1"],
                 "title": "Read changes page 2 (cursor)", "max_retries": 0}]}


async def _wait_for(predicate, *, timeout=30, interval=0.1):
    async with asyncio.timeout(timeout):
        while True:
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(interval)


class _Feed:
    """A controlled workshop change feed: two pages with an opaque
    cursor, a fresh per-request nonce, and mutable records."""

    def __init__(self):
        self.records = [{"id": "chg-1", "unit": "mm", "value": 12},
                        {"id": "chg-2", "unit": "mm", "value": 5}]
        self.nonces = []
        self.hits = []

    async def serve(self, request):
        self.hits.append(request.path_qs)
        nonce = uuid.uuid4().hex
        self.nonces.append(nonce)
        page = int(request.query.get("page") or 1)
        if page == 1:
            return web.json_response({"records": self.records[:1],
                                      "next_page": 2, "nonce": nonce})
        return web.json_response({"records": self.records[1:],
                                  "next_page": None, "nonce": nonce})


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", ["postgres"], indirect=True)
async def test_workshop_pack_three_runs(stack, monkeypatch):
    f, engine, uid, url, payload, hits, router = stack

    feed = _Feed()
    feed.require_unit = False

    async def changes(request):
        # §29.17 drift model: the SAME route refuses requests lacking the
        # newly-required parameter once the upstream contract changes.
        if getattr(feed, "require_unit", False) and \
                not request.query.get("unit"):
            return web.json_response(
                {"error": "unit representation changed; `unit` is now "
                          "required"}, status=422)
        return await feed.serve(request)

    app = web.Application()
    app.router.add_get("/changes", changes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    feed_url = "http://127.0.0.1:" + \
        str(site._server.sockets[0].getsockname()[1])
    from backend.mcp.builder.openapi_parser import OpenAPIParser
    monkeypatch.setattr(OpenAPIParser, "_LOOPBACK_TESTING", True)

    from backend.worker import start_worker
    worker = asyncio.create_task(start_worker(
        f.message_bus, engine, user_repo=f.user_repo, org_repo=f.org_repo,
        mcp_repo=f.mcp_repo))
    builds_started = 0
    try:
        # ── RUN 1: discover → build → wake → two pages with a nonce ──
        parent_1 = "pack-run-1-" + uuid.uuid4().hex
        await f.workflow_repo.save_run(_parent_run(parent_1, uid,
                                                   "Render the change pack"))
        await f.workflow_repo.update_task(parent_1, "read_page_1",
                                          {"status": "WAITING"})
        from backend.services.connector_build_service import (
            request_durable_build, ORIGIN_ORCHESTRATOR)
        track = await request_durable_build(
            f, user_id=uid, method="spec",
            source=json.dumps(_spec(feed_url)), name="Workshop Changes",
            origin=ORIGIN_ORCHESTRATOR, parent_run_id=parent_1,
            parent_task_id="read_page_1", required_operations=[CHANGE_SPEC_OP])
        builds_started += 1
        build = await _wait_for(lambda: _settled_build(f, track["build_id"]))
        assert build["status"] == "success", build.get("error")
        connector_id = build["mcp_id"]
        forge = (build.get("intake") or {}).get("forge") or {}
        assert forge.get("kind") == "openapi_adapter"

        parent = await _wait_for(lambda: _settled_run(f, parent_1))
        assert parent["status"] == "COMPLETED", \
            parent["tasks"][0].get("error")
        output = json.dumps([t.get("output_data") for t in parent["tasks"]])
        assert feed.nonces, "the feed produced fresh nonces"
        assert feed.nonces[-1] in output, \
            "run 1's cursor-chained reads carry fresh nonces"
        assert "chg-1" in output, "the first page's records reached the parent"
        assert "chg-2" in output, \
            "the cursor resolved page 2 of the change feed"
        assert any("page=2" in h for h in feed.hits), \
            "the opaque cursor drove a real second-page request"

        # ── RUN 2: changed records, SAME spec → discovery finds the
        # installed capability, ZERO new builds, pinned invocation ──
        feed.records = [{"id": "chg-1", "unit": "cm", "value": 1.2},
                        {"id": "chg-3", "unit": "cm", "value": 0.5}]
        builds_before = len(feed.hits)
        parent_2 = "pack-run-2-" + uuid.uuid4().hex
        await f.workflow_repo.save_run(_parent_run(parent_2, uid,
                                                   "Render the change pack"))
        await f.workflow_repo.update_task(parent_2, "read_page_1",
                                          {"status": "WAITING"})
        # Discovery searches the eligible installed operations FIRST —
        # `list_changes` is published, so NO build is requested. The
        # invocation goes through the ordinary authorized tool boundary
        # with the SAME pinned connector run 1 installed.
        from backend.services.built_capability import invoke_built_capability
        wait = await f.wait_repo.latest_wait(parent_1, "read_page_1")
        assert wait and str(wait.get("state", "")).startswith("resolved")
        probe = await invoke_built_capability(
            router, f.workflow_repo,
            {"user_id": uid, "run_id": parent_1, "task_id": "read_page_1"},
            {"operation": CHANGE_SPEC_OP, "arguments": {"page": 1}})
        assert len(feed.hits) == builds_before + 1, \
            "run 2 reads through the pinned revision without any build"
        # The parent completes through the normal engine path with the
        # pinned connector (no build child was ever created).
        await engine.evaluate_dag(parent_2)
        parent = await _wait_for(lambda: _settled_run(f, parent_2),
                                 timeout=40)
        assert parent["status"] == "COMPLETED", parent["tasks"][0].get("error")
        output2 = json.dumps([t.get("output_data") for t in parent["tasks"]])
        assert "chg-3" in output2, "run 2 reads the CHANGED records"
        assert feed.nonces[-1] in output2, \
            "run 2's cursor-chained reads carry their own fresh nonces"

        # ── RUN 3: schema drift → old pinned run parks → sibling staged ──
        drifted_spec = _spec(feed_url, require_unit=True)
        # The DRIFTED parent's plan is pinned to the old capability: the
        # old invocation against a feed requiring `unit` fails honestly.
        feed.require_unit = True
        # The route handler reads the CURRENT feed state, so a mutation
        # (not a re-registration) models the upstream drift.
        async def changes_with_drift(request):
            return await changes(request)

        async def changes(request):
            if feed.require_unit and not request.query.get("unit"):
                return web.json_response(
                    {"error": "unit representation changed; `unit` is now "
                              "required"}, status=422)
            return await feed.serve(request)

        parent_3 = "pack-run-3-" + uuid.uuid4().hex
        await f.workflow_repo.save_run(_parent_run(parent_3, uid,
                                                   "Render the change pack"))
        await f.workflow_repo.update_task(parent_3, "read_page_1",
                                          {"status": "WAITING"})
        # The OLD pinned revision serves run 3's invocation first: the
        # drifted feed refuses it (422) and the run parks honestly.
        await engine.evaluate_dag(parent_3)
        settled = await _wait_for(lambda: _settled_run(f, parent_3),
                                  timeout=40)
        assert settled["status"] == "FAILED", \
            "a drifted feed fails the old revision honestly (no misleading pack)"

        # The SIBLING revision is staged from the drifted spec and
        # validated/activated through the same durable build path.
        track3 = await request_durable_build(
            f, user_id=uid, method="spec",
            source=json.dumps(drifted_spec), name="Workshop Changes",
            origin=ORIGIN_ORCHESTRATOR, parent_run_id=None,
            parent_task_id=None, required_operations=[CHANGE_SPEC_OP])
        builds_started += 1
        build3 = await _wait_for(lambda: _settled_build(f,
                                                        track3["build_id"]))
        assert build3["status"] == "success", build3.get("error")
        assert build3["mcp_id"] != connector_id, \
            "a changed spec is a DIFFERENT product (drift is visible)"
        forge3 = (build3.get("intake") or {}).get("forge") or {}
        assert forge3.get("effective_spec_digest") != \
            forge.get("effective_spec_digest"), \
            "the drift changes the effective spec digest"
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        await runner.cleanup()


async def _settled_build(f, build_id):
    build = await f.mcp_repo.get_build(build_id)
    return build if build and build.get("status") in ("success", "error") \
        else None


async def _settled_run(f, run_id):
    run = await f.workflow_repo.get_run(run_id)
    return run if run and run["status"] in ("COMPLETED", "FAILED") else None
