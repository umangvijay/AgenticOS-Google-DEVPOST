"""v5.6 review Phase-A regressions: membership acceptance, reservation
mechanics, browser gates, and session leases."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.api.dependencies.auth import AuthenticatedUser
from backend.api.routers.orgs import add_member, AddMemberRequest
from backend.models.resume import Resume
from backend.services.web_agent import WebAgent
from backend.services.browser_sessions import (
    BrowserSession, put_session, get_session, sweep_expired_sessions, close_session,
)


def _user(uid: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=uid, email=f"{uid}@x", name=uid, role="user", auth_provider="local")


def _session(**overrides) -> BrowserSession:
    base = dict(playwright=None, browser=None, context=None, page=None,
                allowed_domains={"example.com"}, secrets={}, goal="g", history=[],
                steps=[], step_num=1, max_steps=5, challenge_type="otp", url="https://example.com")
    base.update(overrides)
    return BrowserSession(**base)


# ── A1: the members endpoint cannot enroll directly ───────────────────

@pytest.mark.asyncio
async def test_a1_members_endpoint_never_creates_membership():
    org_repo = SimpleNamespace(
        get_org=AsyncMock(return_value={"org_id": "org-1", "name": "Acme", "owner_id": "owner"}),
        get_member_role=AsyncMock(side_effect=lambda oid, uid: {"owner": "owner"}.get(uid) if oid == "org-1" else None),
        get_by_email=AsyncMock(return_value=None),
        create_invitation=AsyncMock(return_value={"invitation_id": "i1", "org_id": "org-1",
                                                  "email": "n@x", "role": "member", "expires_at": "e"}),
        add_member=AsyncMock(return_value=True),
    )
    factory = SimpleNamespace(
        org_repo=org_repo, audit_repo=AsyncMock(),
        user_repo=SimpleNamespace(get_by_email=AsyncMock(return_value=None)),
    )
    request = SimpleNamespace(client=None, app=SimpleNamespace(state=SimpleNamespace(factory=factory)))
    result = await add_member("org-1", AddMemberRequest(email="n@x", role="member"), request, _user("owner"))
    assert result["status"] == "invitation_pending"
    assert result["token"]
    # The smoking gun: membership creation is never invoked by this route.
    org_repo.add_member.assert_not_called()


# ── A3: atomic reservations ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_a3_reservation_refuses_over_cap_and_settles(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.usage_repository import SQLiteUsageRepository
    db = DatabaseManager(str(tmp_path / "a3.db"))
    await db.initialize()
    try:
        repo = SQLiteUsageRepository(db)
        assert await repo.reserve_usage("u", "2026-10-31", 800, cap=1000) is True
        # Racing near the cap: the second reservation must lose.
        assert await repo.reserve_usage("u", "2026-10-31", 800, cap=1000) is False
        assert await repo.get_committed_usage("u", "2026-10-31") == 800
        await repo.settle_usage("u", "2026-10-31", 800, 300)
        assert await repo.get_committed_usage("u", "2026-10-31") == 300
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a3_reservations_are_durable_across_connections(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.usage_repository import SQLiteUsageRepository
    db1 = DatabaseManager(str(tmp_path / "a3b.db"))
    await db1.initialize()
    writer = SQLiteUsageRepository(db1)
    await writer.reserve_usage("u", "2026-10-31", 400, cap=1000)
    await writer.settle_usage("u", "2026-10-31", 400, 250)
    # A separate connection (the "other process") sees the settled spend.
    db2 = DatabaseManager(str(tmp_path / "a3b.db"))
    await db2.initialize()
    reader = SQLiteUsageRepository(db2)
    assert await reader.get_committed_usage("u", "2026-10-31") == 250
    await db2.close()
    await db1.close()


# ── A6: browser consequential-action classification ───────────────────

def test_a6_form_submit_is_consequential():
    assert WebAgent._is_consequential({"action": "type", "submit": True}, {"elements": []}) is True
    assert WebAgent._is_consequential({"action": "type"}, {"elements": []}) is False


def test_a6_submit_button_click_is_consequential():
    snapshot = {"elements": [{"idx": 1, "submit": True}, {"idx": 2, "submit": False}]}
    assert WebAgent._is_consequential({"action": "click", "index": 1}, snapshot) is True
    assert WebAgent._is_consequential({"action": "click", "index": 2}, snapshot) is False
    effect = WebAgent._describe_effect({"action": "click", "index": 1}, {
        "url": "https://example.com/checkout", "elements": [{"idx": 1, "text": "Pay now"}]})
    assert "Pay now" in effect and "example.com/checkout" in effect


# ── A7: browser session ownership + leases ────────────────────────────

@pytest.mark.asyncio
async def test_a7_close_session_without_registry_entry_is_a_safe_noop():
    await close_session("run-x", "task-x")  # must not raise


@pytest.mark.asyncio
async def test_a7_sweep_closes_only_expired_leases():
    fresh = _session()
    put_session("run-fresh", "task-fresh", fresh)
    stale = _session()
    stale.created_at = time.time() - 4000
    put_session("run-stale", "task-stale", stale)
    swept = await sweep_expired_sessions(lease_seconds=1800)
    assert swept == 1
    assert get_session("run-fresh", "task-fresh") is not None
    assert get_session("run-stale", "task-stale") is None
    await close_session("run-fresh", "task-fresh")


# ── A8: artifact evidence binds to immutable content ──────────────────

def test_a8_editing_server_code_invalidates_protocol_evidence():
    from backend.mcp.standalone_packaging import (
        package_standalone_server, record_protocol_test, mark_deployable,
        REQUIRED_PROTOCOL_TESTS,
    )
    artifact = package_standalone_server(
        manifest={"mcp_id": "x"}, tools=[{"tool_name": "t"}],
        server_code="server = 1", spec_provenance="official_spec")
    for test in REQUIRED_PROTOCOL_TESTS:
        artifact = record_protocol_test(artifact, test, passed=True)
    # Someone edits the server code after the tests ran.
    artifact["server_code"] = "server = 2  # tampered"
    with pytest.raises(ValueError, match="different content"):
        mark_deployable(artifact)


def test_a8_dependency_lock_is_exact_versions():
    from backend.mcp.standalone_packaging import package_standalone_server
    artifact = package_standalone_server(
        manifest={"mcp_id": "x"}, tools=[], server_code="s", spec_provenance="official_spec")
    for name, version in artifact["dependency_lock"].items():
        assert version and not any(ch in version for ch in "<>*^~"), (name, version)
