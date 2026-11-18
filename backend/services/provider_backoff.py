"""Provider retry backoff (Phase 3 §3.16).

Bounded exponential backoff with jitter and Retry-After awareness, shared by
every provider path (Gemini candidate loops, Groq/HuggingFace fallbacks).
A provider throttling ALL workers must produce bounded waiting — never a
synchronized retry storm. Callers re-check Phase 2 allowance on every new
potentially billable attempt (the metered-call wrapper does this: each
attempt holds its own reservation).
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from backend.config.settings import settings

logger = logging.getLogger(__name__)

MAX_BACKOFF_ATTEMPTS = 3   # attempt index cap: delay stops growing beyond here

# §3.20 retry-volume observability: counters only, labels carry provider and
# the error class name — never model prompts or provider response bodies.
RETRY_METRICS: dict[str, int] = {}


def retry_metrics() -> dict[str, int]:
    return dict(RETRY_METRICS)


def extract_retry_after(exc: BaseException) -> Optional[float]:
    """Best-effort Retry-After (seconds) from a provider exception. Never
    trusts anything but numeric values; provider bodies are not parsed for
    anything besides a bare number (no error text is echoed into logs)."""
    for attr in ("retry_after_seconds", "retry_after"):
        value = getattr(exc, attr, None)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    return None


async def provider_backoff(exc: BaseException, attempt: int) -> float:
    """Sleep a bounded, jittered delay before the next provider attempt.

    - exponential: base * 2^attempt, clamped to PROVIDER_RETRY_MAX_DELAY_SECONDS
    - Retry-After aware: never shorter than the provider asked (also clamped)
    - jittered: ±25% so parallel workers desynchronize
    Returns the delay actually applied (for metrics/logs).
    """
    base = max(0.0, settings.PROVIDER_RETRY_BASE_DELAY_SECONDS)
    ceiling = max(base, settings.PROVIDER_RETRY_MAX_DELAY_SECONDS)
    attempt = max(0, min(int(attempt), MAX_BACKOFF_ATTEMPTS))
    delay = min(ceiling, base * (2 ** attempt))
    retry_after = extract_retry_after(exc)
    if retry_after is not None:
        delay = min(ceiling, max(delay, retry_after))
    actual = delay * random.uniform(1.0, 1.25) if delay else 0.0
    key = f"provider_backoff{{provider={getattr(exc, 'provider', 'unknown')},error={type(exc).__name__}}}"
    RETRY_METRICS[key] = RETRY_METRICS.get(key, 0) + 1
    if actual > 0:
        await asyncio.sleep(actual)
    return actual
