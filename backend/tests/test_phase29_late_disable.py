"""Phase 29 revision — late disable AFTER parent wake, BEFORE invocation
(§29.17).

The race window between a parent's wake (its wait resolved, capability
reported ready) and the actual connector invocation is fenced by the
eligibility recheck at invocation: disabling the connector in that
window refuses the call, fails the run honestly, and the API receives
ZERO unauthorized hits. Also pins the persisted Forge record for both
build kinds.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress
from types import SimpleNamespace

import pytest

from backend.tests.test_phase26_connector_journey import stack  # noqa: F401

SPEC_OP = "list_changes"


async def _wait_for(predicate, *, timeout=25, interval=0.1):
    """Poll an async predicate until it returns a truthy value."""
    async with asyncio.timeout(timeout):
        while True:
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(interval)


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", ["postgres"], indirect=True)
async def test_disable_after_parent_wake_refuses_invocation_with_zero_extra_hits(
        stack):
    f, engine, uid, url, payload, hits, router = stack
    parent_id = "parent-" + uuid.uuid4().hex
    await f.workflow_repo.save_run({"run_id": parent_id, "user_id": uid,
        "goal": "Read the change feed", "status": "RUNNING", "tasks": [{
            "task_id": "read", "run_id": parent_id, "status": "WAITING",
            "agent": "core.connector_call",
            "input_data": {"operation": SPEC_OP, "arguments": {}},
            "title": "Read changes", "max_retries": 0}]})
    await f.workflow_repo.update_task(parent_id, "read", {"status": "WAITING"})
    spec = {"openapi": "3.0.0", "info": {"title": "ChangeFeed", "version": "1.0"},
        "servers": [{"url": url}], "paths": {"/entries": {"get": {
            "operationId": SPEC_OP, "responses": {"200": {"description": "ok"}}}}}}
    from backend.services.connector_build_service import (
        request_durable_build, ORIGIN_ORCHESTRATOR)
    track = await request_durable_build(
        f, user_id=uid, method="spec", source=json.dumps(spec),
        name="ChangeFeed", origin=ORIGIN_ORCHESTRATOR,
        parent_run_id=parent_id, parent_task_id="read",
        required_operations=[SPEC_OP])

    from backend.worker import start_worker
    worker = asyncio.create_task(start_worker(
        f.message_bus, engine, user_repo=f.user_repo, org_repo=f.org_repo,
        mcp_repo=f.mcp_repo))
    probe_hits_seen = 0
    disabled_at_wake = False
    try:
        # Wait for the parent WAKE (the wait resolves) — the connector is
        # validated, activated, and the parent has been told to continue.
        async def _woken():
            nonlocal probe_hits_seen
            wait = await f.wait_repo.latest_wait(parent_id, "read")
            build = await f.mcp_repo.get_build(track["build_id"])
            if build.get("mcp_id") and wait and \
                    str(wait.get("state", "")).startswith("resolved"):
                return build
            return None
        build = await _wait_for(_woken)
        assert build and build["status"] == "success"
        connector_id = build["mcp_id"]
        connector = await f.mcp_repo.get_mcp(connector_id)
        assert connector["is_enabled"] is True, \
            "the parent only wakes for a published, enabled revision"
        probe_hits_seen = len(hits)
        assert probe_hits_seen >= 1, "the factory probe reached the real API"

        # DISABLE inside the wake→invoke window.
        await f.mcp_repo.set_mcp_enabled(connector_id, False)
        disabled_at_wake = True

        # The parent's invocation must be REFUSED: no extra API hits, and
        # the run fails honestly naming the disabled connector.
        async def _settled():
            parent = await f.workflow_repo.get_run(parent_id)
            return parent if parent["status"] in ("FAILED", "COMPLETED") \
                else None
        parent = await _wait_for(_settled, timeout=40)
        assert parent["status"] == "FAILED", parent["status"]
        blob = json.dumps(parent["tasks"][0].get("error") or "") + \
            json.dumps(parent.get("error") or "")
        assert "disabled" in blob.lower(), blob[:200]
        assert len(hits) == probe_hits_seen, \
            "zero unauthorized API hits after the late disable"
        assert disabled_at_wake is True
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", ["postgres"], indirect=True)
async def test_forge_record_persists_kind_and_operation_digests(stack):
    """§29.15: the durable build row carries the Forge record — resolved
    source URI, raw vs effective digests, kind, and per-operation
    definition digests with effect classes."""
    f, engine, uid, url, payload, hits, router = stack
    spec = {"openapi": "3.0.0", "info": {"title": "Feed", "version": "1.0"},
        "servers": [{"url": url}], "paths": {"/entries": {"get": {
            "operationId": SPEC_OP, "responses": {"200": {"description": "ok"}}},
            "post": {"operationId": "add_change",
                     "responses": {"201": {"description": "created"}}}}}}
    from backend.services.connector_build_service import (
        request_durable_build, ORIGIN_API)
    track = await request_durable_build(
        f, user_id=uid, method="spec", source=json.dumps(spec),
        name="Feed", origin=ORIGIN_API)
    from backend.worker import start_worker
    worker = asyncio.create_task(start_worker(
        f.message_bus, engine, user_repo=f.user_repo, org_repo=f.org_repo,
        mcp_repo=f.mcp_repo))
    try:
        async def _done():
            build = await f.mcp_repo.get_build(track["build_id"])
            return build if build.get("status") in ("success", "error") \
                else None
        build = await _wait_for(_done)
        assert build["status"] == "success", build.get("error")
        forge = (build.get("intake") or {}).get("forge") or {}
        assert forge.get("kind") == "openapi_adapter"
        assert forge.get("effective_spec_digest"), \
            "the normalized spec digest is recorded"
        assert forge.get("raw_digest"), "the raw source digest is recorded"
        ops = {op["id"]: op for op in forge.get("operations", [])}
        assert ops[SPEC_OP]["effect_class"] == "read"
        assert ops["add_change"]["effect_class"] == "write"
        for op in ops.values():
            assert op.get("definition_digest"), \
                "each selected operation carries its immutable digest"
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
