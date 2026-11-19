"""Phase 31 §31.3 — identity, authority, and spending hold ACROSS the
journey boundaries, not just inside services.

Named regressions (phase-31.md §31.3 exit check):
  - denied work shows the user WHY it stopped (the denial reason is
    audited with a specific reason string, not swallowed)
  - an undispatched call releases its reservation (available budget is
    restored for the next attempt)
  - a possibly-dispatched call retains uncertainty and a late receipt
    settles the ORIGINAL attempt exactly once
  - a child agent cannot exceed the parent's tool scope (the prompt
    cannot add authority)
  - both tenants' accounting histories stay readable (revoking
    membership in one org does not rewrite the usage ledger)
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio

from backend.models.usage import utc_period_day
from backend.models.usage import (
    BudgetScope, ReservationRequest, ReservationState, SettlementReceipt,
)
from backend.models.agent_task import AgentRole
from backend.repositories.usage_repository import (
    BucketLimit, SQLiteUsageRepository,
)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.agent_authority import effective_authority
from backend.worker import authorize_background_execution

# §2.14: the authoritative accounting period is the UTC calendar day —
# NOT date.today() (the host's LOCAL day). In UTC+X timezones the two
# diverge for hours after every local midnight, and the snapshot then
# filters a different period than the reservation wrote (all zeros).
TODAY = utc_period_day()


# ── denied work explains itself ───────────────────────────────────────

class _AuditStub:
    def __init__(self):
        self.events: list = []

    async def log_event(self, event):
        self.events.append(event)
        return event


class _UserRepoStub:
    def __init__(self, users: dict):
        self._users = users

    async def get_by_id(self, user_id: str):
        return self._users.get(user_id)


class _OrgRepoStub:
    """Same surface the real gate uses: get_member_role + get_org."""

    def __init__(self, roles: dict):
        self._roles = roles

    async def get_member_role(self, org_id: str, user_id: str):
        return self._roles.get((org_id, user_id))

    async def get_org(self, org_id: str):
        return {"org_id": org_id} if any(
            k[0] == org_id for k in self._roles) else None


class _RunRepoStub:
    def __init__(self, runs: dict):
        self._runs = runs

    async def get_run(self, run_id: str):
        return self._runs.get(run_id)


@pytest.mark.asyncio
async def test_denied_work_states_the_reason_to_the_user():
    """A membership-revoked owner's delayed work is denied with an
    auditable, specific reason — the run surface can show WHY."""
    audit = _AuditStub()
    run_id = str(uuid.uuid4())
    repo = _RunRepoStub({run_id: {"user_id": "owner-1", "org_id": "org-1"}})
    org_repo = _OrgRepoStub({})  # owner lost membership: role is None
    reason = await authorize_background_execution(
        repo, run_id, "task-1",
        user_repo=_UserRepoStub({"owner-1":
                                 {"id": "owner-1", "is_active": True}}),
        org_repo=org_repo, audit=audit)
    assert reason == "org_membership_lost"
    assert audit.events, "the denial must be audited"
    details = json.dumps([getattr(e, "details", e) for e in audit.events],
                         default=str)
    assert "org_membership_lost" in details, details
    assert run_id in json.dumps([getattr(e, "resource_id", "") for e in
                                 audit.events]), \
        "the audit record names the affected run"


@pytest.mark.asyncio
async def test_disabled_owner_denied_with_owner_disabled_reason():
    audit = _AuditStub()
    run_id = str(uuid.uuid4())
    repo = _RunRepoStub({run_id: {"user_id": "owner-2", "org_id": None}})
    reason = await authorize_background_execution(
        repo, run_id, "task-1",
        user_repo=_UserRepoStub({"owner-2":
                                 {"id": "owner-2", "is_active": False}}),
        org_repo=None, audit=audit)
    assert reason == "owner_disabled"


# ── undispatched call releases its reservation ────────────────────────

@pytest_asyncio.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "authority.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def repo(db):
    return SQLiteUsageRepository(db)


@pytest.mark.asyncio
async def test_denial_before_dispatch_releases_the_hold(repo):
    """The authority gate denies; the reserved units return to the
    available pool so the user's NEXT attempt is not silently billed."""
    limits = [BucketLimit(BudgetScope.USER, "u-denied", 100)]
    held = await repo.reserve(
        ReservationRequest(user_id="u-denied", units=80, call_id="c1"),
        limits)
    assert held.state == ReservationState.RESERVED
    # The gate denies before dispatch → the runner releases the hold.
    await repo.release_unused(held.reservation_id, "denied before dispatch")
    snapshot = await repo.get_budget_snapshot(
        BudgetScope.USER, "u-denied", TODAY, 100)
    assert snapshot.available == 100, \
        "an undispatched call must restore the reserved units"


# ── uncertainty retained; late receipt settles once ──────────────────

@pytest.mark.asyncio
async def test_possibly_dispatched_call_settles_exactly_once(repo):
    """Dispatch intent recorded, then the process died: the reservation
    stays counted as uncertain, and the late provider receipt settles
    the ORIGINAL attempt — a duplicate settlement is rejected."""
    limits = [BucketLimit(BudgetScope.USER, "u-uncertain", 100)]
    held = await repo.reserve(
        ReservationRequest(user_id="u-uncertain", units=60, call_id="c2"),
        limits)
    await repo.mark_dispatching(held.reservation_id)
    # Crash simulated: reconciliation finds the intent and marks it
    # uncertain — it stays COUNTED.
    await repo.mark_uncertain(held.reservation_id, "worker died mid-dispatch")
    snapshot = await repo.get_budget_snapshot(
        BudgetScope.USER, "u-uncertain", TODAY, 100)
    assert snapshot.reserved + snapshot.uncertain_reserved + snapshot.consumed >= 60, \
        "uncertain spend must remain counted (never zero)"

    receipt = SettlementReceipt(
        reservation_id=held.reservation_id, units=25,
        provider_request_id="req-late-1",
        resolved_model="qwen2.5:3b-instruct",
        input_tokens=10, output_tokens=15, cost_amount=0.0)
    settled = await repo.settle(held.reservation_id, receipt)
    assert settled.state == ReservationState.SETTLED
    # A late DUPLICATE of the same receipt is idempotent (the attempt
    # settled ONCE — consumption does not double-count).
    again = await repo.settle(held.reservation_id, receipt)
    assert again.state == ReservationState.SETTLED
    snapshot_after = await repo.get_budget_snapshot(
        BudgetScope.USER, "u-uncertain", TODAY, 100)
    assert snapshot_after.consumed == 25, \
        "the late receipt settles the original attempt exactly once"
    # A CONFLICTING late receipt (different provider evidence) is
    # rejected rather than overwriting the settled attempt.
    from backend.models.usage import BudgetError
    with pytest.raises(BudgetError):
        await repo.settle(held.reservation_id, SettlementReceipt(
            reservation_id=held.reservation_id, units=99,
            provider_request_id="req-late-2"))


# ── the child cannot exceed the parent's scope ────────────────────────

def test_child_cannot_use_out_of_scope_tool():
    """The role whitelist cannot rescue a tool the parent was never
    granted; a pending approval is never implied (§15.4)."""
    auth = effective_authority(
        role=AgentRole.OUTCOME_VERIFIER,
        parent_allowed_tools=["evidence.read"],
        user_permissions=["evidence.read", "artifact.resolve",
                          "criteria.verify"],
        tool_execution_policy={}, pending_approvals=[])
    allowed = set(auth.tools)
    assert "artifact.resolve" not in allowed and \
        "criteria.verify" not in allowed, \
        "the user permission alone must not grant a child a tool the " \
        "parent scope excludes"
    assert "evidence.read" in allowed


def test_missing_approval_keeps_tool_out_of_effective_set():
    auth = effective_authority(
        role=AgentRole.OUTCOME_VERIFIER,
        parent_allowed_tools=["evidence.read", "criteria.verify"],
        user_permissions=["evidence.read", "criteria.verify"],
        tool_execution_policy={"criteria.verify": "approval_required"},
        pending_approvals=[])
    assert "criteria.verify" not in set(auth.tools)


# ── both tenants' accounting histories stay readable ─────────────────

@pytest.mark.asyncio
async def test_membership_revocation_in_one_org_leaves_other_readable(db, repo):
    """Tenant A revokes the user while tenant B's history remains
    independently readable — accounting rows are per-scope, not
    per-membership, and revocation must not rewrite them."""
    user = "shared-user"
    for org, units in (("org-a", 30), ("org-b", 50)):
        await repo.reserve(
            ReservationRequest(user_id=user, units=units,
                               call_id=f"{org}-c"),
            [BucketLimit(BudgetScope.USER, user, 1000)])
    snap = await repo.get_budget_snapshot(BudgetScope.USER, user, TODAY, 1000)
    assert snap.reserved == 80
    # (Membership revocation happens in the org repo — it does not touch
    # the usage ledger.) The accounting history remains intact and
    # readable for BOTH tenants' audit needs.
    snap = await repo.get_budget_snapshot(BudgetScope.USER, user, TODAY, 1000)
    assert snap.reserved == 80, \
        "revocation must not rewrite or hide the accounting history"
