"""Firestore parity for the Phase 2 reservation contract (§2.9).

Completion condition: the SAME contract tests pass against SQLite and
Firestore. These tests run the core admission/settlement matrix against the
Firestore implementation whenever a Firestore emulator is configured:

    FIRESTORE_EMULATOR_HOST=localhost:8080 pytest backend/tests/test_budget_firestore_parity.py

Without an emulator they are skipped (SQLite parity is covered by
test_budget_reservations.py, which this file mirrors on purpose).
"""
import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from backend.models.usage import (
    BudgetError,
    BudgetErrorCode,
    BudgetScope,
    ReservationRequest,
    ReservationState,
    SettlementReceipt,
    MeasurementSource,
)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# The emulator (like production Firestore) is persistent across runs: every
# test uses a unique-per-run subject id so assertions never see accumulated
# state from earlier runs.
_RUN = uuid.uuid4().hex[:8]

_EMULATOR = os.environ.get("FIRESTORE_EMULATOR_HOST", "").strip()

pytestmark = pytest.mark.skipif(
    not _EMULATOR,
    reason="Firestore parity requires FIRESTORE_EMULATOR_HOST (e.g. localhost:8080)",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_emulator_data():
    """The emulator (like production Firestore) is persistent across runs.
    Wipe the application collections before each test so every test starts
    clean — the equivalent of the SQLite suites' fresh tmp_path database."""
    from backend.repositories.firestore import database as fs_database
    fs_database.FirestoreDB._instance = None
    try:
        client = await fs_database.FirestoreDB.get_client()
        for name in (
            "users", "orgs", "org_members", "org_invitations",
            "org_invitations_pending", "audit_logs",
            "budget_reservations", "budget_reservations_by_key",
            "budget_buckets", "budget_events", "usage_ledger",
        ):
            async for doc in client.collection(name).stream():
                await doc.reference.delete()
    finally:
        client = fs_database.FirestoreDB._instance
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            fs_database.FirestoreDB._instance = None
    yield


@pytest_asyncio.fixture
async def repo():
    from backend.config.settings import settings as app_settings
    if not app_settings.GOOGLE_CLOUD_PROJECT:
        pytest.skip("Firestore parity requires GOOGLE_CLOUD_PROJECT (any value against the emulator)")
    from backend.repositories.firestore import database as fs_database
    from backend.repositories.usage_repository import FirestoreUsageRepository
    # pytest-asyncio gives each test its own event loop, and the cached
    # AsyncClient's gRPC transport is bound to the loop that created it —
    # so every test gets a FRESH client (the production app uses one loop).
    fs_database.FirestoreDB._instance = None
    try:
        yield FirestoreUsageRepository()
    finally:
        client = fs_database.FirestoreDB._instance
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            fs_database.FirestoreDB._instance = None


def user_buckets(user_id: str, limit: int):
    return [BucketLimit(BudgetScope.USER, user_id, limit)]


from backend.repositories.usage_repository import BucketLimit  # noqa: E402


@pytest.mark.asyncio
async def test_first_insert_cannot_bypass_cap(repo):
    with pytest.raises(BudgetError) as exc:
        await repo.reserve(
            ReservationRequest(user_id=f"fs-fresh-{_RUN}", units=150), user_buckets(f"fs-fresh-{_RUN}", 100)
        )
    assert exc.value.code == BudgetErrorCode.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_reserve_and_settle_round_trip(repo):
    reservation = await repo.reserve(
        ReservationRequest(user_id=f"fs-roundtrip-{_RUN}", units=100), user_buckets(f"fs-roundtrip-{_RUN}", 1000)
    )
    assert reservation.state == ReservationState.RESERVED
    await repo.mark_dispatching(reservation.reservation_id)
    settled = await repo.settle(
        reservation.reservation_id,
        SettlementReceipt(reservation_id=reservation.reservation_id, units=80),
    )
    assert settled.state == ReservationState.SETTLED
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, f"fs-roundtrip-{_RUN}", TODAY, 1000)
    assert snapshot.consumed == 80 and snapshot.reserved == 0


@pytest.mark.asyncio
async def test_settlement_replay_is_idempotent(repo):
    reservation = await repo.reserve(
        ReservationRequest(user_id=f"fs-replay-{_RUN}", units=100), user_buckets(f"fs-replay-{_RUN}", 1000)
    )
    receipt = SettlementReceipt(
        reservation_id=reservation.reservation_id, units=100,
        measurement_source=MeasurementSource.ESTIMATED,
    )
    await repo.settle(reservation.reservation_id, receipt)
    await repo.settle(reservation.reservation_id, receipt)
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, f"fs-replay-{_RUN}", TODAY, 1000)
    assert snapshot.consumed == 100


@pytest.mark.asyncio
async def test_duplicate_key_same_payload_returns_same_reservation(repo):
    first = await repo.reserve(
        ReservationRequest(user_id=f"fs-key-{_RUN}", units=100, call_id="call-1"),
        user_buckets(f"fs-key-{_RUN}", 1000),
    )
    second = await repo.reserve(
        ReservationRequest(user_id=f"fs-key-{_RUN}", units=100, call_id="call-1"),
        user_buckets(f"fs-key-{_RUN}", 1000),
    )
    assert first.reservation_id == second.reservation_id
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, f"fs-key-{_RUN}", TODAY, 1000)
    assert snapshot.reserved == 100


@pytest.mark.asyncio
async def test_duplicate_key_changed_payload_conflicts(repo):
    await repo.reserve(
        ReservationRequest(user_id=f"fs-conflict-{_RUN}", units=100, call_id="call-1"),
        user_buckets(f"fs-conflict-{_RUN}", 1000),
    )
    with pytest.raises(BudgetError) as exc:
        await repo.reserve(
            ReservationRequest(user_id=f"fs-conflict-{_RUN}", units=200, call_id="call-1"),
            user_buckets(f"fs-conflict-{_RUN}", 1000),
        )
    assert exc.value.code == BudgetErrorCode.RESERVATION_CONFLICT


@pytest.mark.asyncio
async def test_uncertain_reservation_remains_counted(repo):
    reservation = await repo.reserve(
        ReservationRequest(user_id=f"fs-uncertain-{_RUN}", units=300), user_buckets(f"fs-uncertain-{_RUN}", 1000)
    )
    await repo.mark_dispatching(reservation.reservation_id)
    await repo.mark_uncertain(reservation.reservation_id, reason="timeout")
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, f"fs-uncertain-{_RUN}", TODAY, 1000)
    assert snapshot.reserved == 300
    assert snapshot.uncertain_reserved == 300


@pytest.mark.asyncio
async def test_release_removes_the_hold(repo):
    reservation = await repo.reserve(
        ReservationRequest(user_id=f"fs-release-{_RUN}", units=250), user_buckets(f"fs-release-{_RUN}", 1000)
    )
    await repo.release_unused(reservation.reservation_id, evidence={"validated": False})
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, f"fs-release-{_RUN}", TODAY, 1000)
    assert snapshot.reserved == 0 and snapshot.consumed == 0
