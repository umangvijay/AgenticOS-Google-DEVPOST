"""Phase 29 — metered dispatch regressions (§29.6).

The per-invocation wrapper must run the SAME governed lifecycle on both
paths: reserve with real bucket enforcement, persist the DISPATCHING
intent BEFORE the provider send, then settle once from the actual
receipt. A post-dispatch failure is UNCERTAIN (the provider may have
consumed), never a pre-dispatch release.
"""
from __future__ import annotations

import sqlite3

import pytest
import pytest_asyncio

from backend.services.metered_model import wrap_model_with_metering


class _Recorder:
    """Records every provider dispatch in order; can fail the dispatch."""

    def __init__(self, fail_dispatch: bool = False):
        self.events: list[str] = []
        self.fail_dispatch = fail_dispatch

    def make_inner(self):
        outer = self

        class _Inner:
            provider = "gemini"

            def generate_content(self, *args, **kwargs):
                outer.events.append("provider_dispatch")
                if outer.fail_dispatch:
                    raise RuntimeError("connection reset after send")
                return _FakeResponse(42)

            async def generate_content_async(self, *args, **kwargs):
                outer.events.append("provider_dispatch_async")
                if outer.fail_dispatch:
                    raise RuntimeError("connection reset after send")
                return _FakeResponse(7)

        return _Inner()


class _FakeResponse:
    def __init__(self, prompt_tokens: int, text: str = "hello"):
        self.candidates = []
        self.usage_metadata = {
            "prompt_token_count": prompt_tokens,
            "candidates_token_count": 0,
        }
        self.text = text


@pytest_asyncio.fixture
async def usage_db(tmp_path, monkeypatch):
    db_path = tmp_path / "usage.db"
    from backend.config.settings import settings as app_settings
    monkeypatch.setattr(app_settings, "SQLITE_DB_PATH", str(db_path))
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.usage_repository import (
        SQLiteUsageRepository, set_usage_repo)
    manager = DatabaseManager(str(db_path))
    await manager.initialize()
    repo = SQLiteUsageRepository(manager)
    set_usage_repo(repo)
    try:
        yield repo
    finally:
        set_usage_repo(None)
        await manager.close()


def _reservation_rows(db_path, user_id: str):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT state, units, measurement_source FROM "
            "budget_reservations WHERE user_id=?",
            (user_id,)).fetchall()
    finally:
        conn.close()


@pytest.mark.asyncio()
async def test_async_path_marks_dispatched_before_provider_send(
        usage_db, tmp_path):
    """§29.6: the dispatch intent is durable BEFORE the provider sees
    the request; a lost response afterwards is UNCERTAIN, not released."""
    recorder = _Recorder(fail_dispatch=True)
    model = wrap_model_with_metering(
        recorder.make_inner(), user_id="u1", run_id="r1", task_id="t1")
    with pytest.raises(RuntimeError):
        async for _response in model.generate_content_async("hello world"):
            pass
    assert recorder.events == ["provider_dispatch_async"]
    rows = _reservation_rows(tmp_path / "usage.db", "u1")
    assert rows, "a post-dispatch failure must leave a reconcilable row"
    assert all(state == "uncertain" for state, _, _ in rows), rows


@pytest.mark.asyncio()
async def test_async_path_settles_once_from_receipt(usage_db, tmp_path):
    recorder = _Recorder()
    model = wrap_model_with_metering(
        recorder.make_inner(), user_id="u2", run_id="r2", task_id="t2")
    responses = [r async for r in model.generate_content_async("hi")]
    assert responses, "the generator yields the model response"
    rows = _reservation_rows(tmp_path / "usage.db", "u2")
    assert rows and rows[0][0] == "settled", rows
    assert rows[0][2] == "provider_reported", rows


def test_sync_path_reserves_before_dispatch_and_settles(usage_db, tmp_path):
    """§29.6: the sync path runs the governed lifecycle — bucket
    enforcement, dispatch intent, then one settlement."""
    recorder = _Recorder()
    model = wrap_model_with_metering(
        recorder.make_inner(), user_id="u3", run_id="r3", task_id="t3")
    response = model.generate_content("hello there")
    assert response is not None
    rows = _reservation_rows(tmp_path / "usage.db", "u3")
    assert rows, "the sync reservation must be durable"
    assert {state for state, _, _ in rows} == {"settled"}, rows
    assert all(u > 0 for _, u, _ in rows), rows
    assert {s for _, _, s in rows} == {"provider_reported"}, rows


def test_sync_path_marks_uncertain_when_dispatch_fails(usage_db, tmp_path):
    recorder = _Recorder(fail_dispatch=True)
    model = wrap_model_with_metering(
        recorder.make_inner(), user_id="u4", run_id="r4", task_id="t4")
    with pytest.raises(RuntimeError):
        model.generate_content("hello")
    rows = _reservation_rows(tmp_path / "usage.db", "u4")
    assert rows and rows[0][0] == "uncertain", rows


def test_sync_path_enforces_the_daily_limit(usage_db, tmp_path, monkeypatch):
    """§29.6: the sync path can no longer bypass the allowance — a
    denied reservation means NO provider dispatch."""
    from backend.config.settings import settings as app_settings
    monkeypatch.setattr(app_settings, "DEFAULT_DAILY_TOKEN_LIMIT", 0)
    recorder = _Recorder()
    model = wrap_model_with_metering(
        recorder.make_inner(), user_id="u5", run_id="r5", task_id="t5")
    with pytest.raises(RuntimeError, match="budget reservation denied"):
        model.generate_content("a reasonably long prompt")
    assert recorder.events == [], \
        "a denied reservation must never reach the provider"


@pytest.mark.asyncio()
async def test_conflicting_settlement_cannot_double_count(usage_db, tmp_path):
    """A second, different receipt on a settled reservation raises
    SETTLEMENT_CONFLICT and leaves consumption unchanged."""
    from backend.models.usage import BudgetErrorCode, SettlementReceipt, \
        MeasurementSource
    from backend.repositories.usage_repository import get_usage_repo
    recorder = _Recorder()
    model = wrap_model_with_metering(
        recorder.make_inner(), user_id="u6", run_id="r6", task_id="t6")
    async for _r in model.generate_content_async("hello"):
        pass
    rows_before = _reservation_rows(tmp_path / "usage.db", "u6")
    assert rows_before and rows_before[0][0] == "settled"
    settled_units = rows_before[0][1]

    conn = sqlite3.connect(str(tmp_path / "usage.db"))
    try:
        res_id = conn.execute(
            "SELECT reservation_id FROM budget_reservations "
            "WHERE user_id='u6'").fetchone()[0]
    finally:
        conn.close()
    repo = get_usage_repo()
    from backend.models.usage import BudgetError
    with pytest.raises(BudgetError) as exc_info:
        await repo.settle(res_id, SettlementReceipt(
            reservation_id=res_id, units=settled_units + 5,
            measurement_source=MeasurementSource.PROVIDER_REPORTED))
    assert exc_info.value.code == BudgetErrorCode.SETTLEMENT_CONFLICT
    rows_after = _reservation_rows(tmp_path / "usage.db", "u6")
    assert rows_after == rows_before, \
        "a conflicting settlement must not double-count"
