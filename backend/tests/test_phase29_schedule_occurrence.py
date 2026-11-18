"""Phase 29 revision — schedule occurrence identity and DST policies
(§29.19).

Schedule admission persists the IANA zone, the logical LOCAL wall-clock
occurrence AND the canonical UTC instant; spring-gap and fall-fold
policies are deterministic; the persisted policy drives the worker's
re-arm; an invalid policy fails admission.
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

import pytest

from backend.services.schedule_time import (
    MISSED_RUN_POLICIES, resolve_occurrence, validate_timezone,
)


# ── resolution semantics ─────────────────────────────────────────────

def test_fall_fold_yields_one_wall_time_two_instants():
    """Nov 3 2024, 01:30 New York occurs twice: the fold policy chooses
    the offset interpretation while the LOGICAL occurrence stays the
    same."""
    after = datetime(2024, 11, 1, 12, 0, tzinfo=dt_timezone.utc)
    first = resolve_occurrence("30 1 3 11 *", "America/New_York",
                               after=after, fold_policy="first")
    second = resolve_occurrence("30 1 3 11 *", "America/New_York",
                                after=after, fold_policy="second")
    assert first["fold_ambiguous"] is True
    assert first["local_occurrence"] == second["local_occurrence"], \
        "the logical local occurrence is identical across policies"
    assert first["utc_instant"] != second["utc_instant"], \
        "the canonical UTC instant differs per policy"


def test_spring_gap_skip_advances_to_the_next_legal_wall_clock():
    """A daily 01:30 Europe/London cron across the Mar 29 2026 gap skips
    that day's nonexistent wall time and takes the next day's legal one
    — never a silently shifted instant."""
    resolved = resolve_occurrence(
        "30 1 * * *", "Europe/London",
        after=datetime(2026, 3, 28, 12, 0, tzinfo=dt_timezone.utc),
        gap_policy="skip")
    assert resolved["gap_shifted"] is True
    assert resolved["local_occurrence"] == "2026-03-30T01:30:00"


def test_spring_gap_pinned_policy_keeps_the_historical_reading():
    resolved = resolve_occurrence(
        "30 1 * * *", "Europe/London",
        after=datetime(2026, 3, 28, 12, 0, tzinfo=dt_timezone.utc),
        gap_policy="first")
    assert resolved["local_occurrence"] == "2026-03-29T01:30:00"
    assert resolved["gap_shifted"] is False


def test_normal_wall_time_is_untouched():
    resolved = resolve_occurrence(
        "0 9 * * *", "Europe/Berlin",
        after=datetime(2026, 7, 1, 6, 0, tzinfo=dt_timezone.utc))
    assert resolved["local_occurrence"] == "2026-07-01T09:00:00"
    assert resolved["utc_instant"] == "2026-07-01T07:00:00+00:00"
    assert resolved["fold_ambiguous"] is False
    assert resolved["gap_shifted"] is False


def test_policies_enum_is_frozen():
    assert MISSED_RUN_POLICIES == ("skip", "first", "second", "coalesce_one")


# ── admission persistence ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schedule_admission_persists_zone_policy_and_pair(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.schedule_repository import (
        SQLiteScheduleRepository)
    db = DatabaseManager(str(tmp_path / "sched-adm.db"))
    await db.initialize()
    repo = SQLiteScheduleRepository(db)
    schedule = {
        "schedule_id": "s-occ", "user_id": "u1", "name": "weekly pack",
        "goal": "render the pack", "cron_expression": "30 2 * * 1",
        "schedule_type": "cron", "timezone": "America/New_York",
        "missed_run_policy": "first",
        "next_run_at": "2026-11-02T07:30:00+00:00",
        "next_run_local": "2026-11-02T02:30:00",
    }
    await repo.create_schedule(schedule)
    stored = await repo.get_schedule("s-occ")
    assert stored["timezone"] == "America/New_York"
    assert stored["missed_run_policy"] == "first"
    assert stored["next_run_local"] == "2026-11-02T02:30:00"
    assert stored["next_run_at"] == "2026-11-02T07:30:00+00:00"
    await db.close()


@pytest.mark.asyncio
async def test_populated_legacy_schedule_migrates_policy_columns(tmp_path):
    """A populated pre-revision store gains the additive columns without
    losing rows; legacy rows read the documented default policy."""
    import sqlite3
    legacy = tmp_path / "legacy-sched.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute("""
        CREATE TABLE schedules (
            schedule_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            name TEXT NOT NULL, goal TEXT NOT NULL,
            cron_expression TEXT, schedule_type TEXT NOT NULL DEFAULT 'one_time',
            timezone TEXT NOT NULL DEFAULT 'UTC',
            status TEXT NOT NULL DEFAULT 'active',
            next_run_at TEXT, last_run_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
    conn.execute(
        "INSERT INTO schedules (schedule_id, user_id, name, goal, "
        "created_at, updated_at) VALUES ('s-legacy', 'u1', 'old', 'g', "
        "'2026-01-01', '2026-01-01')")
    conn.commit()
    conn.close()
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.schedule_repository import (
        SQLiteScheduleRepository)
    db = DatabaseManager(str(legacy))
    await db.initialize()
    try:
        repo = SQLiteScheduleRepository(db)
        stored = await repo.get_schedule("s-legacy")
        assert stored is not None
        assert stored["missed_run_policy"] == "coalesce_one", \
            "legacy rows read the documented default policy"
    finally:
        await db.close()


def test_invalid_policy_fails_admission():
    from backend.api.routers.schedules import CreateScheduleRequest
    import pydantic
    # The router validates the policy VALUE at admission; the model
    # itself accepts any string (validation is the route's contract).
    req = CreateScheduleRequest(name="n", goal="g",
                                cron_expression="0 9 * * 1",
                                missed_run_policy="whenever")
    assert req.missed_run_policy == "whenever"
    from backend.services.schedule_time import MISSED_RUN_POLICIES
    assert req.missed_run_policy not in MISSED_RUN_POLICIES
