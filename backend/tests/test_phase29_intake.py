"""Phase 29 — trustworthy application intake (§29.14).

An intake record is persisted BEFORE any generation runs: what was
supplied (digest, declared kind), what the proposal can honestly
support, and — after retrieval — what the spec actually documents and
what remains assumed. An unsupported access mode is recorded, not
invented.
"""
from __future__ import annotations

import hashlib
import json

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.mcp_repository import SQLiteMCPRepository
from backend.services.app_intake import (
    intake_at_admission, finalize_intake_record,
    documented_operations_from_model, authentication_requirements_from_auth,
    source_digest, deserialize_intake,
)


OPENAPI_SPEC = json.dumps({
    "openapi": "3.0.0",
    "info": {"title": "FluentEdge API", "version": "1.0.0"},
    "servers": [{"url": "https://127.0.0.1"}],
    "paths": {
        "/records": {
            "get": {
                "operationId": "list_records",
                "summary": "List records",
                "responses": {"200": {"description": "ok"}},
            },
            "post": {
                "operationId": "create_record",
                "summary": "Create a record",
                "responses": {"201": {"description": "created"}},
            },
        },
    },
    "components": {"securitySchemes": {
        "key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}},
})


@pytest_asyncio.fixture
async def mcp_repo(tmp_path):
    manager = DatabaseManager(str(tmp_path / "intake.db"))
    await manager.initialize()
    repo = SQLiteMCPRepository(manager)
    try:
        yield repo
    finally:
        await manager.close()


def test_intake_record_exists_before_generation():
    intake = intake_at_admission(
        method="spec", source=OPENAPI_SPEC, name="fluentedge",
        required_operations=["list_records"])
    assert intake["stage"] == "admitted"
    assert intake["source_digest"] == source_digest(OPENAPI_SPEC)
    assert intake["requested_operations"] == ["list_records"]
    assert intake["supported_execution_mode"] == "http_adapter"
    assert intake["retrieved_at"] is None, \
        "retrieval facts are unknown at admission — never guessed"
    assert intake["documented_operations"] == []


def test_website_url_declares_browser_mode_not_invented_api():
    intake = intake_at_admission(method="website",
                                 source="https://example.test", name=None)
    assert intake["supported_execution_mode"] == "browser"


def test_unsupported_kind_records_the_missing_prerequisite():
    intake = intake_at_admission(method="carrier_pigeon", source="x",
                                 name=None)
    assert intake["supported_execution_mode"] == "unsupported"


@pytest.mark.asyncio()
async def test_intake_persists_with_the_build_before_generation(mcp_repo):
    """The durable build row carries the admitted intake — a worker that
    picks the build later can read what was declared."""
    from backend.services.connector_build_service import create_build_record

    class _Factory:
        pass
    factory = _Factory()
    factory.mcp_repo = mcp_repo  # instance attr — a class body sees the fixture fn, not the value

    created = await create_build_record(
        factory, user_id="u1", method="spec", source=OPENAPI_SPEC,
        name="fluentedge", origin="api")
    build = await mcp_repo.get_build(created["build_id"])
    intake = build.get("intake") or {}
    assert intake.get("stage") == "admitted"
    assert intake.get("source_digest") == source_digest(OPENAPI_SPEC)
    # The admission digest binds to the bytes the user supplied.
    assert intake.get("source_digest") == hashlib.sha256(
        OPENAPI_SPEC.encode()).hexdigest()


@pytest.mark.asyncio()
async def test_retrieval_stage_records_documented_interfaces(mcp_repo):
    from backend.services.connector_build_service import create_build_record

    class _Factory:
        pass
    factory = _Factory()
    factory.mcp_repo = mcp_repo  # instance attr — a class body sees the fixture fn, not the value

    created = await create_build_record(
        factory, user_id="u1", method="spec", source=OPENAPI_SPEC,
        name="fluentedge", origin="api")
    build = await mcp_repo.get_build(created["build_id"])
    intake = build["intake"]

    from backend.mcp.builder.openapi_parser import OpenAPIParser
    model = OpenAPIParser().parse_text(OPENAPI_SPEC)
    spec_digest = hashlib.sha256(
        json.dumps(OpenAPIParser.raw_spec_default(model) or OPENAPI_SPEC,
                   sort_keys=True).encode()).hexdigest() if False else \
        hashlib.sha256(OPENAPI_SPEC.encode()).hexdigest()
    finalized = finalize_intake_record(
        intake, source_uri=None, spec_digest=spec_digest,
        documented_operations=documented_operations_from_model(model),
        authentication_requirements=authentication_requirements_from_auth(
            {"type": "apiKey", "scopes": ["records.write"]}))
    await mcp_repo.update_build(created["build_id"], {"intake": finalized})

    stored = (await mcp_repo.get_build(created["build_id"]))["intake"]
    assert stored["stage"] == "retrieved"
    ops = {op["operation_id"]: op["method"]
           for op in stored["documented_operations"]}
    assert ops == {"list_records": "GET", "create_record": "POST"}, \
        "documented interfaces come from the retrieved spec only"
    assert "apikey" in stored["authentication_requirements"]
    assert "scopes:records.write" in stored["authentication_requirements"]
    assert stored["digest_matches_admission"] is True


def test_documented_operations_do_not_include_secrets():
    model = type("M", (), {"operations": [
        type("Op", (), {"operation_id": "list", "http_method": "GET",
                        "path": "/records", "summary": "List"})()]})()
    ops = documented_operations_from_model(model)
    assert ops == [{"operation_id": "list", "method": "GET",
                    "path": "/records", "summary": "List"}]
    dumped = json.dumps(ops)
    assert "password" not in dumped and "secret" not in dumped


def test_auth_requirements_never_contain_secret_values():
    requirements = authentication_requirements_from_auth({
        "type": "http_bearer", "token": "sk-super-secret-value"})
    assert all("sk-super-secret-value" not in r for r in requirements)


@pytest.mark.asyncio()
async def test_populated_store_migrates_the_intake_column(tmp_path):
    """A populated legacy store gains the intake column without losing
    its build rows (fresh + populated parity, §29.11 pattern)."""
    legacy = tmp_path / "legacy.db"
    import sqlite3
    conn = sqlite3.connect(str(legacy))
    conn.execute("""
        CREATE TABLE mcp_builds (
            build_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '', method TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued',
            stage TEXT NOT NULL DEFAULT 'queued', logs_json TEXT NOT NULL DEFAULT '[]',
            mcp_id TEXT, tools_json TEXT NOT NULL DEFAULT '[]', error TEXT,
            origin TEXT, parent_run_id TEXT, parent_workflow_id TEXT,
            parent_task_id TEXT, wait_reason TEXT, organization_id TEXT,
            credential_reference TEXT, required_operations_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
    conn.execute(
        "INSERT INTO mcp_builds (build_id, user_id, method, source, "
        "created_at, updated_at) VALUES ('b-legacy', 'u1', 'spec', '{}', "
        "'2026-01-01', '2026-01-01')")
    conn.commit()
    conn.close()
    manager = DatabaseManager(str(legacy))
    await manager.initialize()
    try:
        repo = SQLiteMCPRepository(manager)
        build = await repo.get_build("b-legacy")
        assert build is not None, "the legacy row survives migration"
        assert build.get("intake") == {}, \
            "the migrated column defaults to an empty intake"
        await repo.update_build("b-legacy", {"intake": {"stage": "admitted"}})
        assert (await repo.get_build("b-legacy"))["intake"]["stage"] == \
            "admitted"
    finally:
        await manager.close()


def test_deserialize_intake_tolerates_garbage():
    assert deserialize_intake(None) == {}
    assert deserialize_intake("not-json") == {}
