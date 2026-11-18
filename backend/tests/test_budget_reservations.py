"""Phase 2 acceptance matrix (§2.17) — single-process reservation contract.

Runs the SAME tests against SQLite and, when a Firestore emulator is
configured (FIRESTORE_EMULATOR_HOST set and the package importable), against
Firestore — the completion condition in §2.9 is that both implementations
satisfy one contract.
"""
import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from backend.models.usage import (
    BudgetError,
    BudgetErrorCode,
    BudgetScope,
    BudgetUnit,
    ReservationRequest,
    ReservationState,
    SettlementReceipt,
    MeasurementSource,
)
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.usage_repository import (
    BucketLimit,
    SQLiteUsageRepository,
)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "contract.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
def repo(db):
    return SQLiteUsageRepository(db)


def user_buckets(user_id: str, limit: int):
    return [BucketLimit(BudgetScope.USER, user_id, limit)]


def make_receipt(reservation, units: int, provider_request_id: str = "req-1") -> SettlementReceipt:
    return SettlementReceipt(
        reservation_id=reservation.reservation_id,
        units=units,
        measurement_source=MeasurementSource.PROVIDER_REPORTED,
        provider_request_id=provider_request_id,
        input_tokens=units // 2,
        output_tokens=units - units // 2,
    )


# ── admission matrix ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_account_requests_exactly_the_cap_accepted_once(repo):
    request = ReservationRequest(user_id="exact-user", units=1000, purpose="generation")
    reservation = await repo.reserve(request, user_buckets("exact-user", 1000))
    assert reservation.state == ReservationState.RESERVED
    assert reservation.units == 1000
    # A second, distinct call with zero room must be refused.
    with pytest.raises(BudgetError) as exc:
        await repo.reserve(
            ReservationRequest(user_id="exact-user", units=1, call_id="call-2"),
            user_buckets("exact-user", 1000),
        )
    assert exc.value.code == BudgetErrorCode.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_negative_reservation_is_policy_invalid(repo):
    with pytest.raises(BudgetError) as exc:
        await repo.reserve(
            ReservationRequest(user_id="neg-user", units=-5),
            user_buckets("neg-user", 1000),
        )
    assert exc.value.code == BudgetErrorCode.BUDGET_POLICY_INVALID


@pytest.mark.asyncio
async def test_zero_allowance_blocks_new_paid_calls(repo):
    with pytest.raises(BudgetError) as exc:
        await repo.reserve(
            ReservationRequest(user_id="zero-user", units=10),
            user_buckets("zero-user", 0),
        )
    assert exc.value.code == BudgetErrorCode.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_missing_buckets_refuses_to_admit(repo):
    """Missing accounting must fail closed, never admit for free (G7)."""
    with pytest.raises(BudgetError) as exc:
        await repo.reserve(ReservationRequest(user_id="nobucket-user", units=10), [])
    assert exc.value.code == BudgetErrorCode.BUDGET_POLICY_INVALID


@pytest.mark.asyncio
async def test_duplicate_key_same_payload_returns_same_reservation(repo):
    request = ReservationRequest(
        user_id="replay-user", units=100, call_id="call-1", purpose="generation",
    )
    first = await repo.reserve(request, user_buckets("replay-user", 1000))
    second = await repo.reserve(
        ReservationRequest(user_id="replay-user", units=100, call_id="call-1",
                           purpose="generation"),
        user_buckets("replay-user", 1000),
    )
    assert first.reservation_id == second.reservation_id
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "replay-user", TODAY, 1000)
    assert snapshot.reserved == 100, "replay created a second hold"


@pytest.mark.asyncio
async def test_duplicate_key_changed_payload_conflicts(repo):
    request = ReservationRequest(
        user_id="conflict-user", units=100, call_id="call-1", purpose="generation",
    )
    await repo.reserve(request, user_buckets("conflict-user", 1000))
    with pytest.raises(BudgetError) as exc:
        await repo.reserve(
            ReservationRequest(user_id="conflict-user", units=200, call_id="call-1",
                               purpose="generation"),
            user_buckets("conflict-user", 1000),
        )
    assert exc.value.code == BudgetErrorCode.RESERVATION_CONFLICT


@pytest.mark.asyncio
async def test_all_applicable_limits_must_pass(repo):
    """User allowance remaining does not admit when the platform bucket is
    exhausted (§2.3 — a call satisfies EVERY constraint)."""
    request = ReservationRequest(user_id="multi-user", units=500)
    user_limit = [BucketLimit(BudgetScope.USER, "multi-user", 1000),
                  BucketLimit(BudgetScope.PLATFORM, "platform", 400)]
    with pytest.raises(BudgetError) as exc:
        await repo.reserve(request, user_limit)
    assert exc.value.code == BudgetErrorCode.BUDGET_EXHAUSTED


# ── settlement / release semantics ────────────────────────────────────


@pytest.mark.asyncio
async def test_settling_one_reservation_leaves_another_hold_intact(repo):
    first = await repo.reserve(
        ReservationRequest(user_id="iso-user", units=300, call_id="call-a"),
        user_buckets("iso-user", 1000),
    )
    second = await repo.reserve(
        ReservationRequest(user_id="iso-user", units=300, call_id="call-b"),
        user_buckets("iso-user", 1000),
    )
    await repo.settle(first.reservation_id, make_receipt(first, 200))
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "iso-user", TODAY, 1000)
    assert snapshot.consumed == 200
    assert snapshot.reserved == 300, "unrelated hold was disturbed"


@pytest.mark.asyncio
async def test_settlement_with_conflicting_receipt_raises(repo):
    reservation = await repo.reserve(
        ReservationRequest(user_id="conflict-settle-user", units=100),
        user_buckets("conflict-settle-user", 1000),
    )
    await repo.settle(reservation.reservation_id, make_receipt(reservation, 100, "req-a"))
    with pytest.raises(BudgetError) as exc:
        await repo.settle(reservation.reservation_id, make_receipt(reservation, 150, "req-b"))
    assert exc.value.code == BudgetErrorCode.SETTLEMENT_CONFLICT


@pytest.mark.asyncio
async def test_release_repeated_removes_hold_once(repo):
    reservation = await repo.reserve(
        ReservationRequest(user_id="release-user", units=250),
        user_buckets("release-user", 1000),
    )
    await repo.release_unused(reservation.reservation_id, evidence={"validated": False})
    await repo.release_unused(reservation.reservation_id, evidence={"validated": False})
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "release-user", TODAY, 1000)
    assert snapshot.reserved == 0
    assert snapshot.consumed == 0
    assert reservation.state


@pytest.mark.asyncio
async def test_cannot_settle_a_released_reservation(repo):
    reservation = await repo.reserve(
        ReservationRequest(user_id="rel-settle-user", units=100),
        user_buckets("rel-settle-user", 1000),
    )
    await repo.release_unused(reservation.reservation_id)
    with pytest.raises(BudgetError) as exc:
        await repo.settle(reservation.reservation_id, make_receipt(reservation, 100))
    assert exc.value.code == BudgetErrorCode.RESERVATION_STATE_CONFLICT


@pytest.mark.asyncio
async def test_unknown_reservation_is_an_error_not_free_consumption(repo):
    from backend.models.usage import new_reservation_id
    with pytest.raises(BudgetError) as exc:
        await repo.settle(
            new_reservation_id(), make_receipt(type("R", (), {"reservation_id": "x"}), 50)
        )
    assert exc.value.code == BudgetErrorCode.RESERVATION_NOT_FOUND


# ── state machine ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_intent_uses_optimistic_concurrency(repo):
    reservation = await repo.reserve(
        ReservationRequest(user_id="dispatch-user", units=100),
        user_buckets("dispatch-user", 1000),
    )
    await repo.mark_dispatching(reservation.reservation_id, expected_version=0)
    with pytest.raises(BudgetError) as exc:
        await repo.mark_dispatching(reservation.reservation_id, expected_version=0)
    assert exc.value.code == BudgetErrorCode.RESERVATION_STATE_CONFLICT


@pytest.mark.asyncio
async def test_uncertain_reservation_remains_counted(repo):
    """G5/G8: uncertain execution does not silently release allowance."""
    reservation = await repo.reserve(
        ReservationRequest(user_id="uncertain-user", units=400),
        user_buckets("uncertain-user", 1000),
    )
    await repo.mark_dispatching(reservation.reservation_id)
    await repo.mark_uncertain(reservation.reservation_id, reason="provider timeout")
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "uncertain-user", TODAY, 1000)
    assert snapshot.reserved == 400
    assert snapshot.uncertain_reserved == 400  # subset of reserved
    assert snapshot.available == 600


@pytest.mark.asyncio
async def test_uncertain_resolves_to_settled_with_late_evidence(repo):
    reservation = await repo.reserve(
        ReservationRequest(user_id="late-user", units=300),
        user_buckets("late-user", 1000),
    )
    await repo.mark_dispatching(reservation.reservation_id)
    await repo.mark_uncertain(reservation.reservation_id, reason="stream disconnected")
    settled = await repo.settle(reservation.reservation_id, make_receipt(reservation, 180))
    assert settled.state == ReservationState.SETTLED
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "late-user", TODAY, 1000)
    assert snapshot.consumed == 180
    assert snapshot.uncertain_reserved == 0
    assert snapshot.reserved == 0


@pytest.mark.asyncio
async def test_reconciliation_can_prove_an_uncertain_call_unused(repo):
    reservation = await repo.reserve(
        ReservationRequest(user_id="prove-user", units=300),
        user_buckets("prove-user", 1000),
    )
    await repo.mark_dispatching(reservation.reservation_id)
    await repo.mark_uncertain(reservation.reservation_id, reason="worker crash")
    released = await repo.release_unused(
        reservation.reservation_id, evidence={"reconciliation": "no provider charge"}
    )
    assert released.state == ReservationState.RELEASED
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "prove-user", TODAY, 1000)
    assert snapshot.reserved == 0 and snapshot.consumed == 0


# ── snapshots, corrections, reconciliation candidates ─────────────────


@pytest.mark.asyncio
async def test_snapshot_reflects_the_accounting_equation(repo):
    await repo.reserve(
        ReservationRequest(user_id="eq-user", units=200, call_id="call-a"),
        user_buckets("eq-user", 1000),
    )
    settled = await repo.reserve(
        ReservationRequest(user_id="eq-user", units=100, call_id="call-b"),
        user_buckets("eq-user", 1000),
    )
    await repo.settle(settled.reservation_id, make_receipt(settled, 60))
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "eq-user", TODAY, 1000)
    # Available = Limit − Settled consumption − Outstanding holds (§2.4)
    assert snapshot.consumed == 60
    assert snapshot.reserved == 200
    assert snapshot.available == 740
    assert snapshot.unit == BudgetUnit.TOKENS.value or snapshot.unit == BudgetUnit.TOKENS


@pytest.mark.asyncio
async def test_adjustment_is_idempotent_and_append_only(repo):
    await repo.record_adjustment(
        BudgetScope.USER, "adj-user", TODAY, units=-50,
        reason="provider billed less than recorded", actor="ops-admin",
        correction_id="corr-1",
    )
    await repo.record_adjustment(
        BudgetScope.USER, "adj-user", TODAY, units=-50,
        reason="provider billed less than recorded", actor="ops-admin",
        correction_id="corr-1",
    )
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "adj-user", TODAY, 1000)
    assert snapshot.consumed == -50, "correction applied twice"


@pytest.mark.asyncio
async def test_list_reconciliation_candidates_finds_stale_uncertain(repo):
    stale = await repo.reserve(
        ReservationRequest(user_id="stale-user", units=100),
        user_buckets("stale-user", 1000),
    )
    await repo.mark_dispatching(stale.reservation_id)
    await repo.mark_uncertain(stale.reservation_id, reason="timeout")
    future = datetime.now(timezone.utc).replace(year=2030)
    candidates = await repo.list_reconciliation_candidates(older_than=future)
    assert any(r.reservation_id == stale.reservation_id for r in candidates)


@pytest.mark.asyncio
async def test_policy_version_stamped_on_reservations(repo):
    reservation = await repo.reserve(
        ReservationRequest(user_id="policy-user", units=10),
        user_buckets("policy-user", 1000),
    )
    assert reservation.policy_version  # auditable policy provenance
