from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.engine.idempotency_guard import IdempotencyGuard
from backend.mcp.tool_policy import ToolPolicy
from backend.mcp.tool_router import _as_manifest, _as_tool, ToolRouter, ToolRouterError
from backend.services.tool_validation import record_read_probe, credential_binding_id_from
from backend.tests.test_readiness_regressions import ledger


@pytest.mark.asyncio
async def test_remote_success_followed_by_commit_failure_cannot_replay(ledger, monkeypatch):
    guard = IdempotencyGuard(ledger)
    effect = AsyncMock(return_value={"receipt": "remote-123"})
    monkeypatch.setattr(ledger, "commit_success", AsyncMock(side_effect=RuntimeError("store down")))
    result = await guard.execute_once("wf", "t", "send", {}, effect)
    assert result.blocked and result.executed
    key = guard.compute_key("wf", "t", "send", {})
    assert (await ledger.get_record(key))["status"] == "uncertain"
    await ledger.reset_failed_task("wf", "t")
    assert (await guard.execute_once("wf", "t", "send", {}, effect)).blocked
    effect.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_after_possible_remote_effect_is_quarantined(ledger):
    guard = IdempotencyGuard(ledger)
    effect = AsyncMock(side_effect=TimeoutError("receipt lost"))
    with pytest.raises(TimeoutError):
        await guard.execute_once("wf", "t", "send", {}, effect)
    await ledger.reset_failed_task("wf", "t")
    assert (await guard.execute_once("wf", "t", "send", {}, effect)).blocked
    effect.assert_awaited_once()


def connector():
    manifest = {"mcp_id": "m", "name": "API", "owner": "u", "version": "1", "endpoint": "internal://openapi/m",
                "transport": "internal", "trust_tier": "verified", "is_enabled": True, "spec_hash": "spec-v1"}
    tool = {"tool_name": "list", "description": "List", "input_schema": {}, "mcp_id": "m", "mcp_version": "1", "risk_level": 1,
            "operation": {"http_method": "GET", "path": "/items", "servers": [{"url": "https://example.com"}]}}
    # Normalize defaults before hashing, just as the factory does. The
    # probe's evidence binds to the credential CONTEXT derived from the
    # manifest auth (§6.7/§6.12) — here the no-auth binding.
    binding = credential_binding_id_from(manifest.get("auth") or {})
    tool = record_read_probe(_as_tool(tool).model_dump(mode="json"), "spec-v1", binding)
    return manifest, tool


def allowed(manifest, tool):
    return ToolPolicy().is_allowed(_as_tool(tool), _as_manifest(manifest)).allowed


def test_pending_review_never_executes_even_when_enabled():
    manifest, tool = connector()
    assert allowed(manifest, tool)
    manifest["trust_tier"] = "pending_review"
    assert not allowed(manifest, tool)


@pytest.mark.parametrize("change", ["path", "schema", "revision", "credential", "write", "risk"])
def test_definition_or_credential_changes_invalidate_probe(change):
    manifest, tool = connector()
    if change == "path": tool["operation"]["path"] = "/other"
    if change == "schema": tool["input_schema"] = {"required": ["id"]}
    if change == "revision": manifest["spec_hash"] = "spec-v2"
    if change == "credential": manifest["auth"] = {"type": "API_KEY", "credential_ref": "new-key"}
    if change == "write": tool["operation"]["http_method"] = "POST"
    if change == "risk": tool["risk_level"] = 4
    assert not allowed(manifest, tool)


def test_one_passing_operation_does_not_validate_another():
    manifest, tested = connector()
    untested = deepcopy(tested)
    untested["tool_name"] = "getAccount"
    assert allowed(manifest, tested)
    assert not allowed(manifest, untested)


@pytest.mark.asyncio
async def test_direct_tool_execution_enforces_owner():
    manifest, tool = connector()
    repo = SimpleNamespace(get_mcp=AsyncMock(return_value=manifest), get_cached_tools=AsyncMock(return_value=[tool]))
    router = ToolRouter(repo, ToolPolicy())
    with pytest.raises(ToolRouterError, match="not accessible"):
        await router.execute_tool("m__list", {}, {"user_id": "other"})
    repo.get_cached_tools.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("expired,expected", [(True, True), (False, False)])
async def test_firestore_claim_respects_lease_expiry(monkeypatch, expired, expected):
    """Transaction body unit test; emulator contention still belongs to release acceptance."""
    from backend.repositories.firestore import workflow_repository as module
    expiry = datetime.now(timezone.utc) + timedelta(seconds=-60 if expired else 60)
    data = {"tasks": [{"task_id": "t", "status": "RUNNING", "attempt": 1, "lease_expires_at": expiry.isoformat()}]}
    from unittest.mock import Mock
    ref = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(exists=True, to_dict=lambda: deepcopy(data))))
    transaction = SimpleNamespace(update=Mock())
    db = SimpleNamespace(collection=lambda _: SimpleNamespace(document=lambda _: ref), transaction=lambda: transaction)
    monkeypatch.setattr(module.firestore, "async_transactional", lambda f: f)
    repo = module.FirestoreWorkflowRepository()
    monkeypatch.setattr(repo, "_get_db", AsyncMock(return_value=db))
    assert await repo.claim_task("run", "t") is expected
    if expected:
        assert transaction.update.call_args.args[1]["tasks"][0]["attempt"] == 2
    else:
        transaction.update.assert_not_called()
