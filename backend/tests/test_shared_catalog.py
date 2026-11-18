"""
AgentOS — Shared integration catalog tests (audit §9 "publish user-built MCPs")

The marketplace gate: only verified integrations may be published, the
catalog view never leaks spec JSON or auth material, and installing
clones the integration with a per-user id and NO copied credentials.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.api.routers.integrations import (
    install_from_catalog,
    list_catalog,
    publish_integration,
    unpublish_integration,
)
from backend.api.dependencies.auth import AuthenticatedUser


def _user(uid: str, role: str = "user") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=uid, email=f"{uid}@x", name=uid, role=role, auth_provider="local")


def _factory_with(mcps: dict, tools: dict):
    mcp_repo = SimpleNamespace(
        get_mcp=AsyncMock(side_effect=lambda mid: mcps.get(mid)),
        set_public=AsyncMock(side_effect=lambda mid, flag: mcps[mid].__setitem__("is_public", flag) or True),
        list_public_mcps=AsyncMock(side_effect=lambda: [m for m in mcps.values() if m.get("is_public")]),
        get_cached_tools=AsyncMock(side_effect=lambda mid: tools.get(mid, [])),
        register_mcp=AsyncMock(),
        cache_tools=AsyncMock(),
    )
    factory = SimpleNamespace(
        mcp_repo=mcp_repo,
        audit_repo=SimpleNamespace(log_event=AsyncMock()),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(factory=factory)))
    return request, mcp_repo


VERIFIED = {
    "mcp_id": "good1", "name": "Petstore", "description": "demo",
    "owner": "alice", "trust_tier": "verified", "is_public": False,
    "source_type": "url", "spec_version": "1.0.0", "updated_at": "2026-10-17T00:00:00Z",
    "spec_hash": "abc123", "auth": {"type": "API_KEY", "in": "header", "name": "X-API-Key", "credential_ref": "petstore-key"},
    "spec_json": '{"secret": "spec"}', "is_enabled": True,
}
PENDING = {
    "mcp_id": "pend1", "name": "Unverified", "owner": "alice",
    "trust_tier": "pending_review", "is_public": False, "auth": {}, "is_enabled": True,
}
TOOLS = {
    "good1": [{"tool_name": "list_pets", "description": "List pets"}],
    "good2": [],
}


@pytest.mark.asyncio
async def test_publish_requires_owner():
    mcps = {"good1": dict(VERIFIED)}
    request, repo = _factory_with(mcps, TOOLS)
    with pytest.raises(Exception) as e:
        await publish_integration("good1", request, _user("mallory"))
    assert "not found" in str(e.value.detail)
    repo.set_public.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_requires_verified_tier():
    mcps = {"pend1": dict(PENDING)}
    request, repo = _factory_with(mcps, TOOLS)
    with pytest.raises(Exception) as e:
        await publish_integration("pend1", request, _user("alice"))
    assert "verified" in e.value.detail
    repo.set_public.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_verified_sets_public():
    mcps = {"good1": dict(VERIFIED)}
    request, repo = _factory_with(mcps, TOOLS)
    result = await publish_integration("good1", request, _user("alice"))
    assert result["is_public"] is True
    repo.set_public.assert_awaited_with("good1", True)


@pytest.mark.asyncio
async def test_catalog_never_leaks_spec_or_auth():
    mcps = {"good1": dict(VERIFIED, is_public=True)}
    request, _ = _factory_with(mcps, TOOLS)
    result = await list_catalog(request, _user("bob"))
    entry = result["integrations"][0]
    blob = str(entry)
    assert entry["name"] == "Petstore"
    assert entry["tools"][0]["name"] == "list_pets"
    assert "spec_json" not in entry and "secret" not in blob
    assert "credential_ref" not in blob and "petstore-key" not in blob
    assert "alice" not in blob  # owner identity stays private


@pytest.mark.asyncio
async def test_unpublish_by_admin_allowed():
    mcps = {"good1": dict(VERIFIED, is_public=True)}
    request, repo = _factory_with(mcps, TOOLS)
    result = await unpublish_integration("good1", request, _user("root", role="admin"))
    assert result["is_public"] is False


@pytest.mark.asyncio
async def test_install_clones_with_per_user_id_and_no_credentials():
    mcps = {"good1": dict(VERIFIED, is_public=True)}
    request, repo = _factory_with(mcps, TOOLS)
    result = await install_from_catalog("good1", request, _user("bob"))
    assert result["installed"] is True and result["tools"] == 1

    clone = repo.register_mcp.await_args.args[0]
    assert clone["mcp_id"] != "good1" and clone["mcp_id"] == result["mcp_id"]
    assert clone["owner"] == "bob"
    assert clone["is_public"] is False
    # Auth *placement* carries over; the owner's credential reference does not.
    assert clone["auth"].get("name") == "X-API-Key"
    assert "credential_ref" not in clone["auth"]
    # Tools re-keyed to the clone
    rekeyed = repo.cache_tools.await_args.args
    assert rekeyed[0] == clone["mcp_id"] and rekeyed[1][0]["mcp_id"] == clone["mcp_id"]


@pytest.mark.asyncio
async def test_install_is_idempotent():
    mcps = {"good1": dict(VERIFIED, is_public=True)}
    request, repo = _factory_with(mcps, TOOLS)
    first = await install_from_catalog("good1", request, _user("bob"))
    # second call finds the clone
    mcps[first["mcp_id"]] = dict(VERIFIED, mcp_id=first["mcp_id"], owner="bob", is_public=False)
    second = await install_from_catalog("good1", request, _user("bob"))
    assert second["installed"] is False
