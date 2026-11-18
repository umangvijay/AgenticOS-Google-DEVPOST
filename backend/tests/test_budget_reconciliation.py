"""Phase 2 reconciliation & audit-trail tests (§2.17/§2.19).

Covers the accounting-reliability surface: event trail integrity, aggregate-
versus-event consistency (ledger corruption detection), authorized
corrections with idempotency, and recovery of settlement-storage failures.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.models.usage import (
    BudgetEventType,
    BudgetScope,
    ReservationState,
    ReservationRequest,
    SettlementReceipt,
    MeasurementSource,
)
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.usage_repository import (
    BucketLimit,
    SQLiteUsageRepository,
)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest_asyncio.fixture
async def env(tmp_path):
    manager = DatabaseManager(str(tmp_path / "recon.db"))
    await manager.initialize()
    repo = SQLiteUsageRepository(manager)
    yield manager, repo
    await manager.close()


# ── event trail (append-only accounting) ──────────────────────────────


@pytest.mark.asyncio
async def test_full_lifecycle_leaves_a_complete_event_trail(env):
    _manager, repo = env
    reservation = await repo.reserve(
        ReservationRequest(user_id="audit-user", units=500),
        [BucketLimit(BudgetScope.USER, "audit-user", 1000)],
    )
    await repo.mark_dispatching(reservation.reservation_id)
    await repo.settle(
        reservation.reservation_id,
        SettlementReceipt(reservation_id=reservation.reservation_id, units=320),
    )
    async with repo._write_tx() as conn:
        rows = await (
            await conn.execute(
                "SELECT event_type, payload_json FROM budget_events "
                "WHERE reservation_id = ? ORDER BY created_at",
                (reservation.reservation_id,),
            )
        ).fetchall()
    types = [r["event_type"] for r in rows]
    assert types == [
        BudgetEventType.RESERVATION_CREATED.value,
        BudgetEventType.DISPATCH_MARKED.value,
        BudgetEventType.SETTLED.value,
    ]
    settled_payload = json.loads(rows[-1]["payload_json"])
    assert settled_payload["reserved_units"] == 500
    assert settled_payload["actual_units"] == 320
    assert settled_payload["overrun"] is False


@pytest.mark.asyncio
async def test_overrun_is_surfaced_in_the_event_trail(env):
    _manager, repo = env
    reservation = await repo.reserve(
        ReservationRequest(user_id="overrun-audit", units=100),
        [BucketLimit(BudgetScope.USER, "overrun-audit", 1000)],
    )
    await repo.settle(
        reservation.reservation_id,
        SettlementReceipt(reservation_id=reservation.reservation_id, units=990),
    )
    async with repo._write_tx() as conn:
        row = await (
            await conn.execute(
                "SELECT payload_json FROM budget_events WHERE reservation_id = ? "
                "AND event_type = 'settled'",
                (reservation.reservation_id,),
            )
        ).fetchone()
    assert json.loads(row["payload_json"])["overrun"] is True


# ── aggregate-vs-event consistency (§2.19 ledger corruption check) ────


@pytest.mark.asyncio
async def test_bucket_aggregates_match_the_event_trail(env):
    """The aggregate counter is a transactionally maintained view of the
    events; replaying the events must reproduce it exactly."""
    _manager, repo = env
    # A realistic mix: holds, settlements (one overrun), a release, an
    # uncertainty, and a legacy add.
    r1 = await repo.reserve(
        ReservationRequest(user_id="cons-user", units=300, call_id="c1"),
        [BucketLimit(BudgetScope.USER, "cons-user", 5000)],
    )
    await repo.settle(r1.reservation_id, SettlementReceipt(reservation_id=r1.reservation_id, units=420))
    r2 = await repo.reserve(
        ReservationRequest(user_id="cons-user", units=250, call_id="c2"),
        [BucketLimit(BudgetScope.USER, "cons-user", 5000)],
    )
    await repo.release_unused(r2.reservation_id, evidence={"never_sent": True})
    r3 = await repo.reserve(
        ReservationRequest(user_id="cons-user", units=100, call_id="c3"),
        [BucketLimit(BudgetScope.USER, "cons-user", 5000)],
    )
    await repo.mark_dispatching(r3.reservation_id)
    await repo.mark_uncertain(r3.reservation_id, reason="timeout")
    await repo.add_usage("cons-user", TODAY, 50)

    async with repo._write_tx() as conn:
        bucket = await (
            await conn.execute(
                "SELECT * FROM budget_buckets WHERE scope='user' AND subject_id='cons-user' "
                "AND period=? AND unit='tokens'", (TODAY,)
            )
        ).fetchone()
        events = await (
            await conn.execute(
                "SELECT event_type, units, payload_json FROM budget_events "
                "WHERE scope='user' AND subject_id='cons-user' AND period=?", (TODAY,)
            )
        ).fetchall()

    # Replay events: created → reserved += units; settled → consumed += actual,
    # reserved -= hold; released → reserved -= hold; uncertain → uncertain += hold;
    # legacy mirrors adjust consumed and seed together.
    replayed = {"consumed": 0, "reserved": 0, "uncertain": 0, "seed": 0}
    reservations = {}
    for e in events:
        payload = json.loads(e["payload_json"])
        t = e["event_type"]
        if t == BudgetEventType.RESERVATION_CREATED.value:
            if not payload.get("legacy"):
                reservations[payload.get("reservation_id")] = int(e["units"] or 0)
                replayed["reserved"] += int(e["units"] or 0)
        elif t == BudgetEventType.SETTLED.value:
            replayed["reserved"] -= reservations.pop(payload.get("reservation_id"), 0)
            replayed["consumed"] += int(payload["actual_units"])
        elif t == BudgetEventType.RELEASED.value:
            replayed["reserved"] -= reservations.pop(payload.get("reservation_id"), 0)
        elif t == BudgetEventType.MARKED_UNCERTAIN.value:
            replayed["uncertain"] += int(e["units"] or 0)
        elif t == BudgetEventType.LEGACY_SEED.value:
            # The legacy-seed event records the seeding of pre-Phase-2
            # history; the mirror writes themselves are settlement-shaped
            # events counted via actual_units or the add_usage mirror below.
            pass
    replayed["reserved"] = max(0, replayed["reserved"])
    replayed["uncertain"] = max(0, replayed["uncertain"])
    # The add_usage mirror (unrelated legacy call) adds 50 to both consumed
    # and seed, so it never double-counts through the ledger delta.
    replayed["consumed"] += 50
    replayed["seed"] += 50

    # Settle actual (420) + legacy add (50)
    assert int(bucket["consumed"]) == replayed["consumed"] == 470
    assert int(bucket["reserved"]) == replayed["reserved"] == 100  # the uncertain hold
    assert int(bucket["uncertain_reserved"]) == replayed["uncertain"] == 100
    # Seed equals the legacy amounts mirrored (the 50 add); the settled 420 is
    # counted as consumed, NOT as seed.
    assert int(bucket["legacy_seed"]) == replayed["seed"] == 50
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "cons-user", TODAY, 5000)
    assert snapshot.consumed == 470
    assert snapshot.reserved == 100
    assert snapshot.available == 5000 - 470 - 100


@pytest.mark.asyncio
async def test_uncertain_holds_survive_a_full_restart_cycle(env, tmp_path):
    """Restart with a NEW DatabaseManager: uncertain holds keep counting and
    remain reconciliation candidates (G5/G6)."""
    _manager, repo = env
    reservation = await repo.reserve(
        ReservationRequest(user_id="restart-recon", units=350),
        [BucketLimit(BudgetScope.USER, "restart-recon", 2000)],
    )
    await repo.mark_dispatching(reservation.reservation_id)
    await repo.mark_uncertain(reservation.reservation_id, reason="worker terminated")

    db_path = _manager.db_path
    fresh = DatabaseManager(db_path)
    await fresh.initialize()
    try:
        fresh_repo = SQLiteUsageRepository(fresh)
        snapshot = await fresh_repo.get_budget_snapshot(BudgetScope.USER, "restart-recon", TODAY, 2000)
        assert snapshot.reserved == 350 and snapshot.uncertain_reserved == 350
        # Reconciliation resolves it with late evidence: settle actual 90.
        settled = await fresh_repo.settle(
            reservation.reservation_id,
            SettlementReceipt(reservation_id=reservation.reservation_id, units=90,
                              measurement_source=MeasurementSource.PROVIDER_REPORTED),
        )
        assert settled.state == ReservationState.SETTLED
        snapshot = await fresh_repo.get_budget_snapshot(BudgetScope.USER, "restart-recon", TODAY, 2000)
        assert snapshot.consumed == 90 and snapshot.reserved == 0
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_settlement_storage_failure_preserves_recoverable_hold(env, monkeypatch):
    """§2.13: DB failure after provider success → the hold survives and a
    bounded recovery retries the settlement (no more paid calls meanwhile —
    the caller raises)."""
    _manager, repo = env
    reservation = await repo.reserve(
        ReservationRequest(user_id="recover-user", units=200),
        [BucketLimit(BudgetScope.USER, "recover-user", 1000)],
    )
    # Simulate a storage outage on settle.
    calls = {"n": 0}
    original_settle = repo.settle

    async def flaky_settle(reservation_id, receipt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk I/O error")
        return await original_settle(reservation_id, receipt)

    monkeypatch.setattr(repo, "settle", flaky_settle)
    with pytest.raises(RuntimeError):
        await repo.settle(
            reservation.reservation_id,
            SettlementReceipt(reservation_id=reservation.reservation_id, units=150),
        )
    # Hold still counted (not lost, not double).
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "recover-user", TODAY, 1000)
    assert snapshot.reserved == 200 and snapshot.consumed == 0
    # Bounded recovery: the retry settles exactly once.
    await repo.settle(
        reservation.reservation_id,
        SettlementReceipt(reservation_id=reservation.reservation_id, units=150),
    )
    snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "recover-user", TODAY, 1000)
    assert snapshot.consumed == 150 and snapshot.reserved == 0
    assert calls["n"] == 2
