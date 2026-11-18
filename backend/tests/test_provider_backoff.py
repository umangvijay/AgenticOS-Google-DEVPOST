"""Phase 3 §3.21: provider throttling → bounded backoff, no retry storm (§3.16)."""
import asyncio
import time

import pytest

from backend.services import provider_backoff as pb


class _Throttled(Exception):
    retry_after_seconds = 2.0


@pytest.mark.asyncio
async def test_backoff_is_bounded_and_grows(monkeypatch):
    monkeypatch.setattr(pb.settings, "PROVIDER_RETRY_BASE_DELAY_SECONDS", 0.05)
    monkeypatch.setattr(pb.settings, "PROVIDER_RETRY_MAX_DELAY_SECONDS", 0.4)
    # Deterministic growth: suppress jitter for the monotonic assertion, and
    # use a plain error (no Retry-After) so the EXPONENTIAL path is exercised
    # — a Retry-After above the ceiling clamps every delay to the ceiling.
    monkeypatch.setattr(pb.random, "uniform", lambda a, b: 1.0)
    delays = []
    for attempt in range(6):
        delays.append(await pb.provider_backoff(RuntimeError("retryable"), attempt))
    assert delays[0] < delays[1] < delays[2]
    # Ceiling holds regardless of attempt count (no unbounded growth).
    assert all(d <= 0.4 + 0.001 for d in delays)


@pytest.mark.asyncio
async def test_backoff_honors_retry_after(monkeypatch):
    monkeypatch.setattr(pb.settings, "PROVIDER_RETRY_BASE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(pb.settings, "PROVIDER_RETRY_MAX_DELAY_SECONDS", 5.0)
    started = time.monotonic()
    applied = await pb.provider_backoff(_Throttled(), attempt=0)
    elapsed = time.monotonic() - started
    # Provider asked for 2s: the wait is never SHORTER than that (clamped by ceiling).
    assert applied >= 2.0
    assert elapsed >= 1.9


@pytest.mark.asyncio
async def test_backoff_without_retry_after_is_fast(monkeypatch):
    monkeypatch.setattr(pb.settings, "PROVIDER_RETRY_BASE_DELAY_SECONDS", 0.02)
    monkeypatch.setattr(pb.settings, "PROVIDER_RETRY_MAX_DELAY_SECONDS", 8.0)
    started = time.monotonic()
    await pb.provider_backoff(RuntimeError("plain retryable"), attempt=0)
    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_jitter_desynchronizes_workers(monkeypatch):
    """Two workers hitting the same throttle wait DIFFERENT amounts — the
    §3.16 anti-retry-storm property."""
    monkeypatch.setattr(pb.settings, "PROVIDER_RETRY_BASE_DELAY_SECONDS", 0.03)
    monkeypatch.setattr(pb.settings, "PROVIDER_RETRY_MAX_DELAY_SECONDS", 5.0)
    results = await asyncio.gather(*[
        pb.provider_backoff(RuntimeError("throttled"), 0) for _ in range(8)
    ])
    assert len(set(round(r, 4) for r in results)) > 1
