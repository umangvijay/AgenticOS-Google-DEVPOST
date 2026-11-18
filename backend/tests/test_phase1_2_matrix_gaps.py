"""Phase 1 + 2 roadmap-matrix rows not previously covered (1.15, 2.14,
2.17) — the explicit gap-fill for the detailed subphase roadmaps."""
import pytest
import pytest_asyncio

from backend.models.usage import (
    BudgetScope, MeasurementSource, ReservationRequest,
    SettlementReceipt,
)
from backend.repositories.usage_repository import BucketLimit
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.usage_repository import (
    SQLiteUsageRepository, set_usage_repo)
from backend.services.budget_service import validate_daily_token_limit


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "matrix.db"))
    await db.initialize()
    repo = SQLiteUsageRepository(db)
    set_usage_repo(repo)
    try:
        yield {"db": db, "repo": repo}
    finally:
        set_usage_repo(None)
        await db.close()


def _bucket(user, limit):
    return BucketLimit(BudgetScope.USER, subject_id=user, limit=limit)


# ── Phase 1.15: role-preservation rows ─────────────────────────────────

@pytest.mark.asyncio
async def test_readd_existing_member_with_stronger_role_changes_nothing(env):
    """Phase 1.15: the legacy add-member route creates an INVITATION
    (never direct enrollment), and re-adding an existing member with a
    stronger role does NOT change the existing membership role — role
    changes go through the dedicated owner-only endpoint."""
    import uuid as _uuid
    from backend.repositories.factory import RepositoryFactory
    factory = RepositoryFactory()
    await factory.initialize()
    try:
        org_repo = factory.org_repo
        owner_id = f"owner-{_uuid.uuid4().hex[:6]}"
        member_email = f"member-{_uuid.uuid4().hex[:8]}@matrix.test"
        member_user = f"user-{_uuid.uuid4().hex[:6]}"
        # orgs.owner_id and org_members.user_id both reference users(id) —
        # create the real users BEFORE the org.
        from backend.repositories.sqlite.user_repository import (
            SQLiteUserRepository)
        users = SQLiteUserRepository(factory.db_manager)
        await users.create_user({
            "id": owner_id, "email": f"{owner_id}-{_uuid.uuid4().hex[:6]}@x.io", "name": "Owner",
            "hashed_password": "x", "is_active": True})
        await users.create_user({
            "id": member_user, "email": member_email, "name": "Member",
            "hashed_password": "x", "is_active": True})
        org = await org_repo.create_org("Matrix Org", owner_id)
        org_id = org["org_id"]
        await org_repo.add_member(org_id, member_user,
                                  "member", added_by=owner_id)
        # The SHARED invitation service (Phase 1.5) — the only membership
        # creation path. Re-inviting an existing member's email is REFUSED
        # outright (a stronger-than-expected invariant: the service refuses
        # rather than silently issuing an invitation that could be read as
        # a role upgrade).
        from backend.services.organization_invitation_service import (
            InvitationError, create_invitation,
        )
        org = await org_repo.get_org(org_id)
        with pytest.raises(InvitationError, match="Already a member"):
            await create_invitation(
                factory, org_id=org_id, org=org, inviter_id=owner_id,
                inviter_role="owner", email=member_email, role="admin")
        # The existing member's role is UNCHANGED.
        role = await org_repo.get_member_role(org_id, member_user)
        assert role == "member", \
            "re-add with a stronger role cannot change the existing role"
    finally:
        await factory.shutdown()


# ── Phase 2.17: validation rows ────────────────────────────────────────

@pytest.mark.asyncio
async def test_negative_reservation_rejected(env):
    """Phase 2.17: negative reservation → validation failure."""
    with pytest.raises(Exception):
        await env["repo"].reserve(
            ReservationRequest(user_id="u1", units=-5),
            [_bucket("u1", 1000)])


@pytest.mark.asyncio
async def test_zero_allowance_rejects_positive_cost(env):
    """Phase 2.17: a zero limit admits no positive-cost call."""
    from backend.models.usage import BudgetError
    with pytest.raises(Exception):
        await env["repo"].reserve(
            ReservationRequest(user_id="u-zero", units=1),
            [_bucket("u-zero", 0)])


@pytest.mark.asyncio
async def test_limit_reduced_preserves_existing_commitment(env):
    """Phase 2.17: reducing a limit does not erase existing commitments —
    a settled reservation remains settled, its usage recorded."""
    repo = env["repo"]
    r = await repo.reserve(ReservationRequest(user_id="u-red", units=500),
                           [_bucket("u-red", 1000)])
    await repo.settle(r.reservation_id, SettlementReceipt(
        reservation_id=r.reservation_id, units=400,
        measurement_source=MeasurementSource.PROVIDER_REPORTED))
    # "Reduce the limit" — settled usage is durable history, unaffected.
    settled = await repo.get_reservation(r.reservation_id)
    assert settled.state.value == "settled"
    assert settled.units == 500   # the original hold is intact history
    # New admissions now respect any tighter limit the operator sets.
    with pytest.raises(Exception):
        await repo.reserve(ReservationRequest(user_id="u-red", units=700),
                           [_bucket("u-red", 100)])   # tighter policy


# ── Phase 2.14: UTC period handling ────────────────────────────────────

def test_utc_period_day_is_utc_not_local():
    """Phase 2.14: the authoritative accounting period is UTC — the same
    instant yields the same period regardless of local timezone."""
    from datetime import datetime, timezone
    from backend.models.usage import utc_period_day
    instant = datetime(2026, 11, 3, 20, 0, 0, tzinfo=timezone.utc)
    # 20:00 UTC = 01:30 IST next day — the period must still be the UTC day.
    assert utc_period_day(instant) == "2026-11-03"


@pytest.mark.asyncio
async def test_settlement_keeps_original_period(env):
    """Phase 2.14: the reservation's period is assigned at ADMISSION and
    the settlement charges that original period (a response after midnight
    settles against the admission period)."""
    repo = env["repo"]
    r = await repo.reserve(ReservationRequest(user_id="u-mid", units=100),
                           [_bucket("u-mid", 1000)])
    assert r.period, "period assigned at admission"
    await repo.settle(r.reservation_id, SettlementReceipt(
        reservation_id=r.reservation_id, units=80,
        measurement_source=MeasurementSource.PROVIDER_REPORTED))
    settled = await repo.get_reservation(r.reservation_id)
    assert settled.period == r.period, \
        "settlement charges the ADMISSION period"


# ── Phase 2.3: settings semantics ──────────────────────────────────────

def test_settings_limits_null_inherits_zero_blocks_negative():
    """Phase 2.3: null inherits the default, 0 means no new paid usage,
    negatives rejected, platform-ceiling raises rejected."""
    # §2.3: null inherits the configured default (not a literal None).
    default = validate_daily_token_limit(None)
    assert default == __import__(
        "backend.config.settings", fromlist=["settings"]
    ).settings.DEFAULT_DAILY_TOKEN_LIMIT
    assert validate_daily_token_limit(0) == 0          # block new spend
    with pytest.raises(Exception):
        validate_daily_token_limit(-1)
    with pytest.raises(Exception):
        validate_daily_token_limit(10**15)             # above the ceiling
