"""Phase 27 §27.12 (27.D7) — feedback retention scheduling."""
import pytest


@pytest.mark.asyncio
async def test_retention_sweep_runs_per_store():
    """The sweep calls apply_retention with the configured window and
    returns the removed count (audit C-16)."""
    from backend.worker import run_feedback_retention
    from backend.config.settings import settings
    calls = []

    class _Store:
        def apply_retention(self, days):
            calls.append(days)
            return 3

    original = settings.FEEDBACK_RETENTION_DAYS
    try:
        settings.FEEDBACK_RETENTION_DAYS = 30
        assert await run_feedback_retention(_Store(), start_worker=None) == 3
        assert calls == [30]
    finally:
        settings.FEEDBACK_RETENTION_DAYS = original


@pytest.mark.asyncio
async def test_retention_disabled_returns_zero():
    from backend.worker import run_feedback_retention
    from backend.config.settings import settings

    original = settings.FEEDBACK_RETENTION_DAYS
    try:
        settings.FEEDBACK_RETENTION_DAYS = 0
        calls = []

        class _Store:
            def apply_retention(self, days):
                calls.append(days)
                return 9

        assert await run_feedback_retention(_Store(), None) == 0
        assert calls == [], "a disabled policy must never call the store"
    finally:
        settings.FEEDBACK_RETENTION_DAYS = original


@pytest.mark.asyncio
async def test_retention_daily_guard():
    """One pass per day per worker: the date marker suppresses repeats."""
    from backend.worker import run_feedback_retention
    from datetime import datetime, timezone

    calls = []

    class _Store:
        def apply_retention(self, days):
            calls.append(days)
            return 1

    class _W:
        pass

    from backend.config.settings import settings
    original = settings.FEEDBACK_RETENTION_DAYS
    try:
        settings.FEEDBACK_RETENTION_DAYS = 30
        w = _W()
        assert await run_feedback_retention(_Store(), w) >= 0
        first = getattr(w, "_last_feedback_retention", None)
        assert first is not None, "the date marker must be set after a pass"
        await run_feedback_retention(_Store(), w)
        assert getattr(w, "_last_feedback_retention") == first, \
            "second same-day pass suppressed"
    finally:
        settings.FEEDBACK_RETENTION_DAYS = original


@pytest.mark.asyncio
async def test_incompatible_store_is_a_noop():
    from backend.worker import run_feedback_retention

    class _Noop:
        pass

    assert await run_feedback_retention(_Noop(), None) == 0
    assert await run_feedback_retention(None, None) == 0
