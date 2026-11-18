"""Phase 28 §28.7 — dispatch-time metering identity and receipt
attribution.

The retained suites cover reservation identities (three calls → three
attributed reservations), ledger durability across processes, and the
frontend reconnect/account-switch drills (phase14-reliability). This
file pins the remaining audit gap: a metered reservation names the
RESOLVED provider at dispatch — a receipt that says 'gemini' for a
Bedrock-wrapped model cannot be reconciled against provider records.
"""
import asyncio
import os
import sqlite3

import pytest


@pytest.fixture
def usage_db(tmp_path, monkeypatch):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.usage_repository import (
        SQLiteUsageRepository, set_usage_repo)
    from backend.config.settings import settings as _settings
    _settings.SQLITE_DB_PATH = os.path.join(str(tmp_path), "usage.db")
    db = DatabaseManager(_settings.SQLITE_DB_PATH)
    yield db, _settings.SQLITE_DB_PATH
    monkeypatch.setattr(
        "backend.repositories.usage_repository._usage_repo", None,
        raising=False)
    set_usage_repo(None)


class _FakeModel:
    _provider = "bedrock"

    def generate_content(self, *a, **k):
        class _R:
            usage_metadata = None
            candidates = []
        return _R()


@pytest.mark.asyncio
async def test_reservation_names_the_resolved_provider(usage_db):
    db, db_path = usage_db
    await db.initialize()
    from backend.repositories.usage_repository import (
        SQLiteUsageRepository, set_usage_repo)
    set_usage_repo(SQLiteUsageRepository(db))
    from backend.services.metered_model import wrap_model_with_metering
    wrapped = wrap_model_with_metering(_FakeModel(), user_id="u",
                                       run_id="run-provider", task_id="t")
    assert wrapped._provider == "bedrock", (
        "the wrapper adopts the wrapped model's own provider attribute")
    # The sync path runs its governed lifecycle on a dedicated loop in a
    # worker thread — never inside the caller's running loop.
    await asyncio.to_thread(wrapped.generate_content, "hello")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT provider FROM budget_reservations WHERE run_id='run-provider'"
        ).fetchone()
        assert row is not None
        assert row[0] == "bedrock", row
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_explicit_provider_argument_wins(usage_db):
    db, db_path = usage_db
    await db.initialize()
    from backend.repositories.usage_repository import (
        SQLiteUsageRepository, set_usage_repo)
    set_usage_repo(SQLiteUsageRepository(db))
    from backend.services.metered_model import wrap_model_with_metering
    wrapped = wrap_model_with_metering(_FakeModel(), user_id="u",
                                       run_id="run-explicit", task_id="t",
                                       provider="vertex")
    assert wrapped._provider == "vertex"
    await asyncio.to_thread(wrapped.generate_content, "hello")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT provider FROM budget_reservations WHERE run_id='run-explicit'"
        ).fetchone()
        assert row is not None and row[0] == "vertex"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_unwrapping_does_not_double_meter(usage_db):
    db, _ = usage_db
    await db.initialize()
    from backend.repositories.usage_repository import (
        SQLiteUsageRepository, set_usage_repo)
    set_usage_repo(SQLiteUsageRepository(db))
    from backend.services.metered_model import wrap_model_with_metering
    model = _FakeModel()
    wrapped = wrap_model_with_metering(model, user_id="u", run_id="r",
                                       task_id="t")
    # Wrapping an already-wrapped model returns THE SAME wrapper — a
    # double wrap would double-charge every invocation.
    assert wrap_model_with_metering(wrapped, user_id="u", run_id="r",
                                    task_id="t") is wrapped
