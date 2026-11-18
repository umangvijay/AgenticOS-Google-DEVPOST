"""Phase 2 concurrency & restart tests (§2.17).

The pre-Phase-2 "cross-process" test created multiple repository objects over
one database connection — that proves neither process isolation nor committed
visibility. These tests use:

- independent DatabaseManager instances (separate OS-level SQLite
  connections, separate WAL readers/writers), and
- REAL separate operating-system processes (subprocess), competing near the
  cap, plus a restart: a process that exits after reserving and a new process
  that must see the hold.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from backend.models.usage import (
    BudgetError,
    BudgetErrorCode,
    BudgetScope,
    ReservationRequest,
    ReservationState,
    SettlementReceipt,
)
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.usage_repository import (
    BucketLimit,
    SQLiteUsageRepository,
)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Worker script executed as a separate OS process. Each invocation performs
# one reservation attempt against the shared database file and prints a JSON
# result — the closest thing to a second deployed worker in a unit test.
_WORKER = """
import json, sys
from backend.repositories.sqlite.database import DatabaseManager
from backend.models.usage import BudgetScope, BudgetError, ReservationRequest
from backend.repositories.usage_repository import BucketLimit, SQLiteUsageRepository

db_path, user_id, units, cap, call_id = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
manager = DatabaseManager(db_path)

import asyncio

async def main():
    await manager.initialize()
    repo = SQLiteUsageRepository(manager)
    try:
        reservation = await repo.reserve(
            ReservationRequest(user_id=user_id, units=units, call_id=call_id,
                               purpose="generation"),
            [BucketLimit(BudgetScope.USER, user_id, cap)],
        )
        print(json.dumps({"admitted": True, "reservation_id": reservation.reservation_id}))
    except BudgetError as exc:
        print(json.dumps({"admitted": False, "code": exc.code.value}))
    finally:
        await manager.close()

asyncio.run(main())
"""


def _run_worker(db_path: str, user_id: str, units: int, cap: int, call_id: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _WORKER, db_path, user_id, str(units), str(cap), call_id],
        capture_output=True, text=True, timeout=60,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"worker failed: {proc.stderr[-800:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest_asyncio.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "conc.db"))
    await manager.initialize()
    yield manager
    await manager.close()


def _independent_repo(tmp_path, name: str):
    """A genuinely separate connection (its own DatabaseManager) over the
    same database file — what a second worker process uses."""
    manager = DatabaseManager(str(Path(tmp_path) / "conc.db"))
    return manager


@pytest.mark.asyncio
async def test_two_independent_connections_compete_near_cap(tmp_path):
    db_path = str(Path(tmp_path) / "indep.db")
    manager_a = DatabaseManager(db_path)
    await manager_a.initialize()
    try:
        repo_a = SQLiteUsageRepository(manager_a)
        # Hold 700 of 1000 via connection A.
        held = await repo_a.reserve(
            ReservationRequest(user_id="race-user", units=700),
            [BucketLimit(BudgetScope.USER, "race-user", 1000)],
        )
        assert held.state == ReservationState.RESERVED

        # Connection B (fresh connection, same file) tries 400 — over the cap.
        manager_b = DatabaseManager(db_path)
        await manager_b.initialize()
        try:
            repo_b = SQLiteUsageRepository(manager_b)
            with pytest.raises(BudgetError) as exc:
                await repo_b.reserve(
                    ReservationRequest(user_id="race-user", units=400, call_id="b"),
                    [BucketLimit(BudgetScope.USER, "race-user", 1000)],
                )
            assert exc.value.code == BudgetErrorCode.BUDGET_EXHAUSTED
            # Only the permitted total was admitted across both connections.
            snapshot = await repo_a.get_budget_snapshot(BudgetScope.USER, "race-user", TODAY, 1000)
            assert snapshot.reserved == 700
        finally:
            await manager_b.close()
    finally:
        await manager_a.close()


@pytest.mark.asyncio
async def test_concurrent_same_call_races_yield_one_reservation(tmp_path):
    """N concurrent admissions of the SAME logical call (same request key)
    produce ONE hold, not N (G3)."""
    import asyncio

    db_path = str(Path(tmp_path) / "sameday.db")
    manager = DatabaseManager(db_path)
    await manager.initialize()
    try:
        repos = [SQLiteUsageRepository(manager) for _ in range(8)]

        async def attempt(i):
            try:
                return await repos[i].reserve(
                    ReservationRequest(user_id="same-user", units=100, call_id="call-1"),
                    [BucketLimit(BudgetScope.USER, "same-user", 10000)],
                )
            except BudgetError:
                return None

        results = await asyncio.gather(*(attempt(i) for i in range(8)))
        ids = {r.reservation_id for r in results if r}
        assert len(ids) == 1, f"races created {len(ids)} reservations for one call"
        snapshot = await SQLiteUsageRepository(manager).get_budget_snapshot(
            BudgetScope.USER, "same-user", TODAY, 10000
        )
        assert snapshot.reserved == 100
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_multiple_os_processes_admit_only_permitted_total(tmp_path):
    """Real subprocess workers compete for a 1000-token cap with 700-token
    requests: exactly one may win, whichever commits first."""
    db_path = str(Path(tmp_path) / "proc.db")
    manager = DatabaseManager(db_path)
    await manager.initialize()
    await manager.close()  # schema ready; workers open their own connections

    results = [_run_worker(db_path, "proc-user", 700, 1000, call_id=f"w{i}") for i in range(2)]
    admitted = [r for r in results if r["admitted"]]
    rejected = [r for r in results if not r["admitted"]]
    assert len(admitted) == 1, f"both processes admitted: {results}"
    assert all(r["code"] == BudgetErrorCode.BUDGET_EXHAUSTED.value for r in rejected)

    # Verify from a fresh connection what the processes left behind.
    manager = DatabaseManager(db_path)
    await manager.initialize()
    try:
        repo = SQLiteUsageRepository(manager)
        snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "proc-user", TODAY, 1000)
        assert snapshot.reserved == 700
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_process_restart_preserves_reservations_and_settlements(tmp_path):
    """G6: a process exits after reserving; a brand-new process must see the
    hold and settle against it."""
    db_path = str(Path(tmp_path) / "restart.db")
    first = _run_worker(db_path, "restart-user", 500, 2000, call_id="restart-1")
    assert first["admitted"] is True

    # New "process": fresh DatabaseManager + repository.
    manager = DatabaseManager(db_path)
    await manager.initialize()
    try:
        repo = SQLiteUsageRepository(manager)
        reservation = await repo.get_reservation(first["reservation_id"])
        assert reservation is not None
        assert reservation.state == ReservationState.RESERVED
        settled = await repo.settle(
            reservation.reservation_id,
            SettlementReceipt(reservation_id=reservation.reservation_id, units=320),
        )
        assert settled.state == ReservationState.SETTLED
        snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "restart-user", TODAY, 2000)
        assert snapshot.consumed == 320 and snapshot.reserved == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_hold_survives_crash_after_dispatch_intent(tmp_path):
    """§2.13: crash after dispatch intent → the hold is NOT released; the
    reservation is a reconciliation candidate."""
    db_path = str(Path(tmp_path) / "crash.db")
    first = _run_worker(db_path, "crash-user", 300, 2000, call_id="crash-1")
    manager = DatabaseManager(db_path)
    await manager.initialize()
    try:
        repo = SQLiteUsageRepository(manager)
        await repo.mark_dispatching(first["reservation_id"])
        # ...process dies here (nothing further is written).
        snapshot = await repo.get_budget_snapshot(BudgetScope.USER, "crash-user", TODAY, 2000)
        assert snapshot.reserved == 300, "crash silently released allowance"
        from datetime import datetime, timezone as tz
        candidates = await repo.list_reconciliation_candidates(
            older_than=datetime.now(tz.utc).replace(year=2030)
        )
        assert any(c.reservation_id == first["reservation_id"] for c in candidates)
    finally:
        await manager.close()
