"""Audit F08 acceptance: durable shared budget accounting.

- Usage recorded by one process is visible to a fresh process (restart does
  not reset the daily cap).
- The budget check reflects the durable total even when the in-memory cache
  is empty (the cross-process property).
- Ledger failures are explicit (503), never a silent pass.
"""
from datetime import datetime, timezone

import pytest
import pytest_asyncio

TODAY = datetime.now(timezone.utc).date().isoformat()

from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.usage_repository import (
    SQLiteUsageRepository,
    set_usage_repo,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "usage.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.mark.asyncio
async def test_ledger_accumulates_across_processes(db):
    """Two 'processes' (separate repository instances over the same store)
    both record spend; the total is the sum, not last-write-wins."""
    process_a = SQLiteUsageRepository(db)
    process_b = SQLiteUsageRepository(db)
    await process_a.add_usage("user-x", TODAY, 700)
    await process_b.add_usage("user-x", TODAY, 900)
    # A third process reads the shared total.
    process_c = SQLiteUsageRepository(db)
    assert await process_c.get_usage("user-x", TODAY) == 1600


@pytest.mark.asyncio
async def test_budget_cap_survives_memory_reset(db):
    """The audit's core complaint: 'restarts reset it'. After wiping the
    process-local dict, the durable total still enforces the cap."""
    from backend.services.llm_context import check_user_budget
    from backend.agents.context_manager import context_manager

    repo = SQLiteUsageRepository(db)
    set_usage_repo(repo)
    try:
        await repo.add_usage("user-cap", TODAY, 1200)
        context_manager._usage.clear()  # simulate a fresh process

        class FixedSettings:
            async def get_settings(self, user_id):
                return {"daily_token_limit": 1000}

        budget = await check_user_budget(FixedSettings(), "user-cap")
        assert budget["exceeded"] is True
        assert budget["used"] == 1200
        assert budget["remaining"] == 0
    finally:
        set_usage_repo(None)


@pytest.mark.asyncio
async def test_budget_under_limit_when_durable_total_low(db):
    from backend.services.llm_context import check_user_budget

    repo = SQLiteUsageRepository(db)
    set_usage_repo(repo)
    try:
        await repo.add_usage("user-ok", TODAY, 200)
        context_manager = __import__(
            "backend.agents.context_manager", fromlist=["context_manager"]
        ).context_manager
        context_manager._usage.clear()

        class FixedSettings:
            async def get_settings(self, user_id):
                return {"daily_token_limit": 1000}

        budget = await check_user_budget(FixedSettings(), "user-ok")
        assert budget["exceeded"] is False
        assert budget["used"] == 200
    finally:
        set_usage_repo(None)


@pytest.mark.asyncio
async def test_ledger_failure_is_explicit_not_silent(db, monkeypatch):
    """Audit: budget failures must be explicit. A broken ledger surfaces as
    a 503 rather than silently allowing work."""
    from fastapi import HTTPException
    from backend.services.llm_context import check_user_budget

    class BrokenRepo:
        async def add_usage(self, user_id, day, tokens):
            raise RuntimeError("ledger down")

        async def get_usage(self, user_id, day):
            raise RuntimeError("ledger down")

    set_usage_repo(BrokenRepo())
    try:
        class FixedSettings:
            async def get_settings(self, user_id):
                return {"daily_token_limit": 1000}

        with pytest.raises(HTTPException) as exc:
            await check_user_budget(FixedSettings(), "any-user")
        assert exc.value.status_code == 503
    finally:
        set_usage_repo(None)
