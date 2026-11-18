"""Phase 3 §3.21 regression suite: the distributed rate-limit defects and
guarantees. Two defects drove this file (§3.0):

1. Cleanup unit mismatch — `current_window()` returned a window ORDINAL while
   the worker purged with an EPOCH cutoff, so cleanup deleted ACTIVE windows
   and reset exhausted allowances mid-window.
2. First-record bypass — both backends created the first counter without
   checking the cap, so limit=0 admitted the first request.

Every test here uses INDEPENDENT connections or processes where the guarantee
concerns multiple replicas (§3.21): two repository objects sharing one
connection prove nothing about cross-replica enforcement.
"""
import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

from backend.security.shared_rate_limiter import (
    SQLiteSharedRateRepository,
    current_window,
    window_bounds,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def db(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "rates.db"))
    await db.initialize()
    yield db
    await db.close()


# ── §3.5: first-record admission and input validation ─────────────────

@pytest.mark.asyncio
async def test_first_request_with_zero_allowance_rejected_without_record(db):
    repo = SQLiteSharedRateRepository(db)
    window = current_window(60)
    result = await repo.admit("ip:zero", "auth", window, 0, window_seconds=60)
    assert result.allowed is False
    assert result.count == 0
    # No counter row may be created by a rejected first request.
    cursor = await db.execute("SELECT COUNT(*) FROM rate_limits")
    assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_first_request_with_allowance_one_then_denied(db):
    repo = SQLiteSharedRateRepository(db)
    window = current_window(60)
    assert (await repo.admit("ip:one", "auth", window, 1)).allowed is True
    denied = await repo.admit("ip:one", "auth", window, 1)
    assert denied.allowed is False


@pytest.mark.asyncio
async def test_final_allowance_and_first_denial(db):
    repo = SQLiteSharedRateRepository(db)
    window = current_window(60)
    results = [await repo.admit("ip:3", "auth", window, 3) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[2].count == 3          # honest counter for remaining headers
    assert results[3].count == 3


@pytest.mark.asyncio
async def test_invalid_policy_inputs_rejected(db):
    repo = SQLiteSharedRateRepository(db)
    window = current_window(60)
    with pytest.raises(ValueError):
        await repo.admit("ip:x", "auth", window, -1, window_seconds=60)
    with pytest.raises(ValueError):
        await repo.admit("ip:x", "auth", window, 5, window_seconds=0)


# ── §3.5: racing for the last slot ────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_requests_cannot_exceed_allowance(db):
    """Twenty coroutines race for an allowance of five: exactly five win."""
    repo = SQLiteSharedRateRepository(db)
    window = current_window(60)
    outcomes = await asyncio.gather(*[
        repo.admit("ip:race", "auth", window, 5, window_seconds=60) for _ in range(20)
    ])
    assert sum(1 for r in outcomes if r.allowed) == 5


# ── §3.4: cleanup must never delete a live window ─────────────────────

@pytest.mark.asyncio
async def test_cleanup_during_active_exhausted_window_keeps_enforcing(db):
    """THE regression: worker-style cleanup runs mid-window; the exhausted
    allowance must STAY exhausted until the window actually ends."""
    repo = SQLiteSharedRateRepository(db)
    window = current_window(60)
    assert (await repo.admit("ip:busy", "auth", window, 1)).allowed is True
    assert (await repo.admit("ip:busy", "auth", window, 1)).allowed is False
    await repo.purge_expired(older_than_epoch=int(time.time()))
    # Still rejected: the active counter survived housekeeping.
    assert (await repo.admit("ip:busy", "auth", window, 1)).allowed is False


@pytest.mark.asyncio
async def test_cleanup_with_any_cutoff_never_deletes_active_windows(db):
    """A cutoff in the far past or far future must never free the CURRENT
    window — cleanup_after is always window_end + one retention window."""
    repo = SQLiteSharedRateRepository(db)
    window = current_window(60)
    assert (await repo.admit("ip:live", "auth", window, 1)).allowed is True
    for cutoff in (0, 1, int(time.time()), 4_000_000_000, 9_999_999_999):
        await repo.purge_expired(older_than_epoch=cutoff)
        assert (await repo.admit("ip:live", "auth", window, 1)).allowed is False, \
            f"active counter was deleted by cutoff {cutoff}"


@pytest.mark.asyncio
async def test_old_windows_are_swept(db):
    repo = SQLiteSharedRateRepository(db)
    old_start = window_bounds(60)[0] - 3600     # an hour-old window
    await repo.admit("ip:old", "auth", old_start, 10, window_seconds=60)
    swept = await repo.purge_expired(older_than_epoch=int(time.time()))
    assert swept >= 1
    cursor = await db.execute("SELECT COUNT(*) FROM rate_limits")
    assert (await cursor.fetchone())[0] == 0


# ── §3.4: window representation and key collisions ────────────────────

def test_window_bounds_are_epoch_aligned():
    now = time.time()
    start, end = window_bounds(300, now=now)
    assert end - start == 300
    assert start % 300 == 0
    assert start <= int(now) < end


@pytest.mark.asyncio
async def test_different_intervals_never_share_a_counter(db):
    """§3.4 #2: the interval is part of the key — a 60s and a 3600s policy
    for the same identity/group count independently."""
    repo = SQLiteSharedRateRepository(db)
    start60, _ = window_bounds(60)
    start3600, _ = window_bounds(3600)
    r60 = await repo.admit("user:7", "workflow_create", start60, 10, window_seconds=60)
    r3600 = await repo.admit("user:7", "workflow_create", start3600, 10, window_seconds=3600)
    assert r60.allowed and r3600.allowed
    assert r60.count == 1 and r3600.count == 1


@pytest.mark.asyncio
async def test_non_60_second_window_cleanup(db):
    """A 120-second policy's windows are spared while live and swept after
    their own retention horizon — not the 60-second default's."""
    repo = SQLiteSharedRateRepository(db)
    # Current 120s window: cleanup_after is start+240, always in the future.
    live_start, _ = window_bounds(120)
    await repo.admit("ip:w2", "auth", live_start, 1, window_seconds=120)
    assert await repo.purge_expired(older_than_epoch=int(time.time())) == 0
    # An expired 120s window (an hour old) is swept by the normal worker call.
    old_start = live_start - 3600
    await repo.admit("ip:w2", "auth", old_start, 1, window_seconds=120)
    assert await repo.purge_expired(older_than_epoch=int(time.time())) == 1


@pytest.mark.asyncio
async def test_limit_change_mid_window_does_not_crash_or_reset(db):
    repo = SQLiteSharedRateRepository(db)
    window = current_window(60)
    for _ in range(3):
        await repo.admit("ip:cfg", "auth", window, 3)
    # Operator lowers the limit mid-window: existing count exceeds the new
    # cap, admission is denied, nothing resets or crashes.
    assert (await repo.admit("ip:cfg", "auth", window, 2)).allowed is False
    # Raising the limit mid-window resumes admission from the STORED count —
    # the counter is not wiped, so no allowance is conjured from nowhere.
    r = await repo.admit("ip:cfg", "auth", window, 5)
    assert r.allowed is True and r.count == 4
    assert (await repo.admit("ip:cfg", "auth", window, 5)).allowed is True
    assert (await repo.admit("ip:cfg", "auth", window, 5)).allowed is False


# ── §3.4 #6: clock boundaries and modest clock skew ───────────────────

def test_window_boundaries_under_skew():
    """A ±30s clock skew between replicas maps to the adjacent window at a
    boundary, or the same window mid-window — admission stays monotone and
    never produces a negative or overlapping interval."""
    base = 1_800_000_000          # exactly on a 60s boundary
    start_a, end_a = window_bounds(60, now=base - 30)
    start_b, end_b = window_bounds(60, now=base)
    start_c, end_c = window_bounds(60, now=base + 30)
    assert (start_a, end_a) == (base - 60, base)
    assert (start_b, end_b) == (base, base + 60)
    assert (start_c, end_c) == (base, base + 60)   # +30s skew: same window
    for s, e in ((start_a, end_a), (start_b, end_b), (start_c, end_c)):
        assert 0 < e - s == 60


@pytest.mark.asyncio
async def test_skewed_worker_cutoff_never_frees_the_current_window(db):
    """A replica whose clock is up to one window behind (or ahead) cannot
    make cleanup free the current allowance: cleanup_after carries a full
    retention window of slack."""
    repo = SQLiteSharedRateRepository(db)
    window = current_window(60)
    assert (await repo.admit("ip:skew", "auth", window, 1)).allowed is True
    now = int(time.time())
    for cutoff in (now - 60, now - 30, now, now + 30):
        await repo.purge_expired(older_than_epoch=cutoff)
        assert (await repo.admit("ip:skew", "auth", window, 1)).allowed is False, \
            f"skewed cutoff {cutoff} freed an active counter"


# ── §3.21: independent connections, same store ────────────────────────

@pytest.mark.asyncio
async def test_independent_connections_share_one_allowance(tmp_path):
    """Two DatabaseManager instances = two connections = two 'replicas'.
    (The pre-Phase-3 test shared one connection and proved nothing.)"""
    from backend.repositories.sqlite.database import DatabaseManager
    path = str(tmp_path / "shared.db")
    db_a = DatabaseManager(path)
    db_b = DatabaseManager(path)
    await db_a.initialize()
    try:
        await db_b.initialize()
        repo_a = SQLiteSharedRateRepository(db_a)
        repo_b = SQLiteSharedRateRepository(db_b)
        window = current_window(60)
        first = await repo_a.admit("ip:multi", "auth", window, 2)
        second = await repo_b.admit("ip:multi", "auth", window, 2)
        third_a = await repo_a.admit("ip:multi", "auth", window, 2)
        third_b = await repo_b.admit("ip:multi", "auth", window, 2)
        assert [r.allowed for r in (first, second, third_a, third_b)] == \
            [True, True, False, False]
    finally:
        await db_a.close()
        await db_b.close()


# ── §3.21: independent operating-system processes ─────────────────────

_CHILD_SCRIPT = """
import asyncio, sys
sys.path.insert(0, {root!r})
from backend.repositories.sqlite.database import DatabaseManager
from backend.security.shared_rate_limiter import SQLiteSharedRateRepository, window_bounds

async def main():
    db = DatabaseManager({db_path!r})
    await db.initialize()
    repo = SQLiteSharedRateRepository(db)
    start = {start!r}   # the PARENT's window: the property under test is
                        # cross-process sharing of ONE allowance, not
                        # window-boundary timing luck
    wins = 0
    for _ in range(3):
        r = await repo.admit("ip:proc", "auth", start, 4, window_seconds=60)
        wins += 1 if r.allowed else 0
    print(wins)
    await db.close()

asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_independent_processes_share_one_allowance(tmp_path):
    """Two real OS processes each attempt 3 admissions against an allowance
    of 4 for the same identity: exactly 4 must win across both processes."""
    db_path = str(tmp_path / "proc.db")
    from backend.repositories.sqlite.database import DatabaseManager
    seed = DatabaseManager(db_path)
    await seed.initialize()   # create schema before spawning children
    await seed.close()

    from backend.security.shared_rate_limiter import window_bounds
    start, _ = window_bounds(60)
    script = _CHILD_SCRIPT.format(root=str(REPO_ROOT), db_path=db_path,
                                  start=start)
    procs = [
        subprocess.run([sys.executable, "-c", script], capture_output=True,
                       text=True, timeout=60, cwd=str(REPO_ROOT))
        for _ in range(2)
    ]
    total = 0
    for proc in procs:
        assert proc.returncode == 0, proc.stderr
        total += int(proc.stdout.strip().splitlines()[-1])
    assert total == 4
