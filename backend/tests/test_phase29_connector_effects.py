"""Phase 29 — effect coverage for connector mutations (§29.7).

A consequential connector operation (POST/PUT/PATCH/DELETE) goes through
the effect ledger: one logical write per canonical request, dispatch
intent persisted BEFORE the HTTP call, a lost reply preserved as
OUTCOME_UNKNOWN with automatic redispatch suppressed. A read is never
ledgered; a refused write is CONFIRMED_NOT_APPLIED (proven not applied).
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from backend.models.effect import EffectState
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.effect_repository import (
    SQLiteEffectRepository, set_effect_repo,
)
from backend.mcp.openapi_executor import execute_openapi_tool


@pytest_asyncio.fixture
async def effect_db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "connector-effects.db"))
    await manager.initialize()
    repo = SQLiteEffectRepository(manager)
    set_effect_repo(repo)
    try:
        yield manager
    finally:
        set_effect_repo(None)
        await manager.close()


@pytest.fixture(autouse=True)
def loopback_testing(monkeypatch):
    """The SSRF guard's documented test mode: the destination is a
    controlled local fixture, not a real network host."""
    from backend.mcp.builder.openapi_parser import OpenAPIParser
    monkeypatch.setattr(OpenAPIParser, "_LOOPBACK_TESTING", True)


@pytest.fixture()
def captured(tmp_path):
    """A controlled destination that records every accepted write and can
    drop its reply."""
    import json as _json

    class _Destination:
        def __init__(self):
            self.writes = []
            self.drop_reply = False
            self.status = 200
            self.log_path = tmp_path / "destination.jsonl"

        def accept(self, payload):
            self.writes.append(payload)
            with open(self.log_path, "a") as fh:
                fh.write(_json.dumps(payload, sort_keys=True) + "\n")
            return self.status

    return _Destination()


def _manifest():
    return {"mcp_id": "mcp-fluentedge", "name": "fluentedge",
            "auth": "{}",
            "operation": {"servers": [{"url": "https://127.0.0.1"}]}}


def _tool(method="POST", path="/records"):
    return {"tool_name": "create_record",
            "operation": {"path": path, "http_method": method,
                          "servers": [{"url": "https://127.0.0.1"}]}}


def _patch_transport(monkeypatch, destination, respond=True):
    """Route the pooled client to the controlled destination."""
    from backend.services import http_pool

    class _Resp:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body

        def json(self):
            if self._body is None:
                raise ValueError("no json")
            return self._body

        @property
        def text(self):
            return str(self._body)

        @property
        def is_error(self):
            return self.status_code >= 400

        @property
        def headers(self):
            return {}

    class _Client:
        async def request(self, *, method, url, params, headers, json,
                          timeout):
            destination.accept({"method": method, "url": url,
                                "body": json})
            if destination.drop_reply or not respond:
                raise RuntimeError("connection reset after send")
            return _Resp(destination.status, {"accepted": True})

    class _Pooled:
        async def __aenter__(self):
            return _Client()

        async def __aexit__(self, *exc):
            return False

    async def _get_client():
        return _Client()

    monkeypatch.setattr(http_pool, "get_client", _get_client)


@pytest.mark.asyncio()
async def test_connector_write_is_effect_ledgered_and_applied_once(
        effect_db, captured, monkeypatch):
    _patch_transport(monkeypatch, captured)
    result = await execute_openapi_tool(
        _manifest(), _tool(), {"name": "n1", "value": "fresh-value"},
        user_id="u1")
    assert result["ok"] is True
    assert result["outcome"] == "confirmed_applied"
    assert result["effect_id"]
    assert len(captured.writes) == 1, \
        "exactly one logical write reached the destination"

    # A duplicate submission of the SAME write is deduplicated — the
    # destination never sees a second write.
    duplicate = await execute_openapi_tool(
        _manifest(), _tool(), {"name": "n1", "value": "fresh-value"},
        user_id="u1")
    assert len(captured.writes) == 1, \
        "the dedup boundary must suppress an automatic resend"
    assert duplicate.get("effect_id") == result["effect_id"]


@pytest.mark.asyncio()
async def test_lost_reply_is_unknown_and_not_redispatched(
        effect_db, captured, monkeypatch):
    """The destination accepts a write and DROPS its reply: the outcome is
    unknown, never a false failure that invites a blind retry."""
    _patch_transport(monkeypatch, captured)
    captured.drop_reply = True
    result = await execute_openapi_tool(
        _manifest(), _tool(), {"name": "n1", "value": "v"},
        user_id="u1")
    assert result["ok"] is False
    assert result["outcome"] == "outcome_unknown"
    assert result["reconciliation_required"] is True
    assert result["retry_disposition"] == "none"
    # The write DID reach the destination (unknown ≠ not applied).
    assert len(captured.writes) == 1

    # Redelivering the task performs ZERO further writes.
    redelivered = await execute_openapi_tool(
        _manifest(), _tool(), {"name": "n1", "value": "v"},
        user_id="u1")
    assert redelivered["outcome"] == "outcome_unknown"
    assert len(captured.writes) == 1, \
        "an uncertain outcome must never be auto-redispatched"


@pytest.mark.asyncio()
async def test_provider_refusal_is_confirmed_not_applied(
        effect_db, captured, monkeypatch):
    _patch_transport(monkeypatch, captured)
    captured.status = 422
    result = await execute_openapi_tool(
        _manifest(), _tool(), {"name": "n1"},
        user_id="u1")
    assert result["ok"] is False
    assert result["outcome"] == "confirmed_not_applied", \
        "a 4xx rejection happened before any apply — provably not applied"


@pytest.mark.asyncio()
async def test_server_error_stays_unknown(effect_db, captured, monkeypatch):
    """A 5xx may have applied the write server-side — it is unknown."""
    _patch_transport(monkeypatch, captured)
    captured.status = 502
    result = await execute_openapi_tool(
        _manifest(), _tool(), {"name": "n1"},
        user_id="u1")
    assert result["outcome"] == "outcome_unknown"
    assert result["reconciliation_required"] is True


@pytest.mark.asyncio()
async def test_read_operations_are_never_ledgered(
        effect_db, captured, monkeypatch):
    _patch_transport(monkeypatch, captured)
    result = await execute_openapi_tool(
        _manifest(), _tool(method="GET", path="/records"), {},
        user_id="u1")
    assert result["ok"] is True
    # No effect rows exist for reads.
    rows = await effect_db._connection.execute(
        "SELECT COUNT(*) FROM effects")
    (count,) = await rows.fetchone()
    assert count == 0


@pytest.mark.asyncio()
async def test_different_arguments_are_a_new_write(
        effect_db, captured, monkeypatch):
    _patch_transport(monkeypatch, captured)
    await execute_openapi_tool(_manifest(), _tool(), {"name": "a"},
                               user_id="u1")
    await execute_openapi_tool(_manifest(), _tool(), {"name": "b"},
                               user_id="u1")
    assert len(captured.writes) == 2, \
        "different writes are distinct logical effects"


@pytest.mark.asyncio()
async def test_second_origin_never_receives_first_origin_write(
        effect_db, captured, monkeypatch):
    """§29.7/§29.10: two connectors writing to different destinations
    keep their effects separate — a dedup on one never suppresses the
    other's write."""
    _patch_transport(monkeypatch, captured)
    manifest_a = _manifest()
    manifest_a["mcp_id"] = "mcp-a"
    manifest_a["name"] = "alpha"
    manifest_a["operation"]["servers"] = [{"url": "https://127.0.0.1:9001"}]
    manifest_a["operation"]["path"] = "/records"
    manifest_b = _manifest()
    manifest_b["mcp_id"] = "mcp-b"
    manifest_b["name"] = "beta"
    manifest_b["operation"]["servers"] = [{"url": "https://127.0.0.1:9002"}]
    manifest_b["operation"]["path"] = "/records"
    await execute_openapi_tool(manifest_a, _tool(), {"name": "same"},
                               user_id="u1")
    await execute_openapi_tool(manifest_b, _tool(), {"name": "same"},
                               user_id="u1")
    assert len(captured.writes) == 2
