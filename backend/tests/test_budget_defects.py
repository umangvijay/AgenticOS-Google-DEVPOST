"""Phase 2 entry point (§2.0 / §2.20 step 1): the two known accounting
defects, fixed by the identity-based reservation contract.

Defect 1 — first-insert bypass: the pre-Phase-2 `reserve_usage` attached its
cap guard to `ON CONFLICT ... DO UPDATE`, so a brand-new (user, day) row
admitted any reservation regardless of the cap.

Defect 2 — duplicate settlement: amount-only settlement without reservation
identity replayed consumption twice.

Both are structurally fixed by the Phase 2 contract: admission validates the
cap inside a dedicated write transaction for fresh AND existing buckets
(§2.8), and settlement is keyed by reservation identity with a receipt digest
(§2.5/§2.7). These tests are permanent regression guards.
"""
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from backend.models.usage import (
    BudgetError,
    BudgetErrorCode,
    BudgetScope,
    ReservationRequest,
    SettlementReceipt,
    MeasurementSource,
)
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.usage_repository import (
    BucketLimit,
    SQLiteUsageRepository,
)
from backend.config.settings import settings

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest_asyncio.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "defects.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.mark.asyncio
async def test_first_insert_cannot_bypass_cap(db):
    """Fresh account requesting more than the cap must be rejected (G2)."""
    repo = SQLiteUsageRepository(db)
    request = ReservationRequest(user_id="fresh-user", units=150, purpose="generation")
    with pytest.raises(BudgetError) as exc:
        await repo.reserve(request, [BucketLimit(BudgetScope.USER, "fresh-user", 100)])
    assert exc.value.code == BudgetErrorCode.BUDGET_EXHAUSTED
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "fresh-user", TODAY, limit=100)
    assert snapshot.consumed == 0
    assert snapshot.reserved == 0


@pytest.mark.asyncio
async def test_settlement_is_idempotent_per_reservation(db):
    """Replaying the same settlement must not add consumption twice (G4)."""
    from backend.models.usage import BudgetScope

    repo = SQLiteUsageRepository(db)
    reservation = await repo.reserve(
        ReservationRequest(user_id="dup-user", units=40, purpose="generation"),
        [BucketLimit(BudgetScope.USER, "dup-user", 1000)],
    )
    receipt = SettlementReceipt(
        reservation_id=reservation.reservation_id, units=40,
        measurement_source=MeasurementSource.ESTIMATED,
    )
    await repo.settle(reservation.reservation_id, receipt)
    await repo.settle(reservation.reservation_id, receipt)  # replay
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "dup-user", TODAY, limit=1000)
    assert snapshot.consumed == 40, "settlement replay counted consumption twice"
    assert snapshot.reserved == 0


@pytest.mark.asyncio
async def test_legacy_shim_cannot_bypass_cap_on_first_insert(db):
    """The deprecated amount-only shim also guards fresh rows now."""
    repo = SQLiteUsageRepository(db)
    assert await repo.reserve_usage("legacy-fresh", TODAY, 150, cap=100) is False
    assert await repo.get_committed_usage("legacy-fresh", TODAY) == 0


@pytest.mark.asyncio
async def test_overrun_is_recorded_not_clipped(db):
    """Actual usage above the hold is recorded in full (§2.4/§6.4)."""
    from backend.models.usage import BudgetScope

    repo = SQLiteUsageRepository(db)
    reservation = await repo.reserve(
        ReservationRequest(user_id="over-user", units=100, purpose="generation"),
        [BucketLimit(BudgetScope.USER, "over-user", 1000)],
    )
    await repo.settle(
        reservation.reservation_id,
        SettlementReceipt(reservation_id=reservation.reservation_id, units=240),
    )
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "over-user", TODAY, limit=1000)
    assert snapshot.consumed == 240, "overrun was clipped"
    assert snapshot.reserved == 0
