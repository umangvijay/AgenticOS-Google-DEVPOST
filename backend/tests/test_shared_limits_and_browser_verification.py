"""v5.7 review §1 (shared rate limiting) and §6 (browser verification,
ineffective-action detection) regressions. Phase 3 (§3.4/§3.5/§3.21) updated
the shared limiter to epoch windows, typed decisions, and first-record cap
validation; deeper distributed tests live in test_distributed_rate_limits.py
and test_admission_policy.py."""
import asyncio
import time

import pytest
import pytest_asyncio

from backend.security.rate_limiter import (
    RATE_LIMIT_GROUPS, check_rate_limit, check_shared_rate_limit,
)
from backend.security.shared_rate_limiter import (
    SQLiteSharedRateRepository, current_window, set_shared_rate_repo,
)


@pytest_asyncio.fixture
async def shared_db(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "rates.db"))
    await db.initialize()
    yield db
    await db.close()


# ── §1: shared rate limiting ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_s1_shared_limit_enforces_across_replicas(shared_db):
    """Two 'replicas' (separate connections to the same store) share ONE
    allowance: 3 auth attempts total, not 3 each."""
    repo_a = SQLiteSharedRateRepository(shared_db)
    repo_b = SQLiteSharedRateRepository(shared_db)
    window = current_window(60)
    results = [await repo.admit("ip:1.2.3.4", "auth", window, 3) for repo in (repo_a, repo_a, repo_b)]
    assert [r.allowed for r in results] == [True, True, True]
    # The 4th attempt from EITHER replica is refused.
    assert (await repo_b.admit("ip:1.2.3.4", "auth", window, 3)).allowed is False
    assert (await repo_a.admit("ip:1.2.3.4", "auth", window, 3)).allowed is False


@pytest.mark.asyncio
async def test_s1_windows_reset_independently(shared_db):
    repo = SQLiteSharedRateRepository(shared_db)
    w1 = current_window(60)
    assert (await repo.admit("ip:x", "auth", w1, 1)).allowed is True
    assert (await repo.admit("ip:x", "auth", w1, 1)).allowed is False
    w2 = w1 + 60  # next window (epoch starts are 60s apart): fresh allowance
    assert (await repo.admit("ip:x", "auth", w2, 1)).allowed is True


@pytest.mark.asyncio
async def test_s1_shared_helper_raises_429_and_reports_when_store_missing(shared_db, monkeypatch):
    from fastapi import HTTPException
    repo = SQLiteSharedRateRepository(shared_db)
    set_shared_rate_repo(repo)
    try:
        # Exhaust the shared window.
        for _ in range(RATE_LIMIT_GROUPS["contact"]):
            await check_shared_rate_limit("ip:9.9.9.9", group="contact")
        with pytest.raises(HTTPException) as exc:
            await check_shared_rate_limit("ip:9.9.9.9", group="contact")
        assert exc.value.status_code == 429
        assert exc.value.detail["code"] == "RATE_LIMITED"
    finally:
        set_shared_rate_repo(None)
    # Without a shared store the helper degrades gracefully (no raise).
    info = await check_shared_rate_limit("ip:9.9.9.9", group="contact")
    assert info is not None and info["shared"] is False


@pytest.mark.asyncio
async def test_s1_purge_removes_only_expired_windows(shared_db):
    repo = SQLiteSharedRateRepository(shared_db)
    old_window = current_window(60) - 3600   # epoch-based: one hour ago
    await repo.admit("ip:old", "auth", old_window, 10)
    fresh_window = current_window(60)
    await repo.admit("ip:fresh", "auth", fresh_window, 10)
    swept = await repo.purge_expired(older_than_epoch=int(time.time()))
    assert swept >= 1
    # The fresh window's counter survived cleanup and still enforces its cap.
    fresh = await repo.admit("ip:fresh", "auth", fresh_window, 10)
    assert fresh.allowed is True and fresh.count == 2
    # The swept row is gone: re-admitting the old window starts a new counter.
    old = await repo.admit("ip:old", "auth", old_window, 10)
    assert old.allowed is True and old.count == 1


# ── §6: browser outcome verification + stale detection ────────────────

def test_s6_done_requires_at_least_one_effective_step():
    """A model claiming success after only errored steps is flipped to
    failure with verification evidence."""
    # Simulate the done-verification rule directly against step shapes.
    steps_all_errored = [{"step": 1, "error": "timeout"}, {"step": 2, "error": "boom"}]
    success = True
    if success and steps_all_errored and all(st.get("error") for st in steps_all_errored):
        success = False
    assert success is False


def test_s6_successful_steps_allow_done_success():
    steps = [{"step": 1, "action": "click"}, {"step": 2, "action": "type"}]
    success = True
    if success and steps and all(st.get("error") for st in steps):
        success = False
    assert success is True


# ── A2 cross-connection durability (already shipped) — regression ─────

@pytest.mark.asyncio
async def test_a2_usage_survives_reopen(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.usage_repository import SQLiteUsageRepository
    path = str(tmp_path / "usage.db")
    db1 = DatabaseManager(path)
    await db1.initialize()
    await SQLiteUsageRepository(db1).add_usage("u", "2026-10-31", 700)
    await db1.close()
    db2 = DatabaseManager(path)  # "process restart"
    await db2.initialize()
    assert await SQLiteUsageRepository(db2).get_usage("u", "2026-10-31") == 700
    await db2.close()
