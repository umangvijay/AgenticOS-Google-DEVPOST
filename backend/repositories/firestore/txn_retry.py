"""Contention-tolerant transaction execution (§20.13 hardening).

Firestore ABORTS contended transactions BY DESIGN — callers are expected
to retry with jittered backoff (Firestore transaction best practice).
The client library already retries `Aborted` internally, but concurrent
coroutines retry in LOCKSTEP (same backoff rhythm), which under heavy
same-document contention exhausts the emulator's fixed transaction-lock
window and, on real Firestore, wastes attempts. This helper reruns the
WHOLE transaction with random jitter, breaking lockstep.

Safety: the transaction callbacks in this repository layer are written
as RERUNNABLE (all state is re-read inside the transaction), so rerunning
the callback is exactly the intended retry semantics — no external side
effects occur before commit.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable

logger = logging.getLogger("firestore_txn_retry")

_MAX_OUTER_ATTEMPTS = 5
_BASE_DELAY_MS = 40
_MAX_DELAY_MS = 250


def _is_contention(exc: BaseException) -> bool:
    from google.api_core.exceptions import Aborted, ServiceUnavailable, DeadlineExceeded
    if isinstance(exc, (Aborted, ServiceUnavailable, DeadlineExceeded)):
        return True
    # The async client exhausts its internal attempts and re-raises as
    # ValueError("Failed to commit transaction in N attempts.").
    text = str(exc)
    return "Failed to commit transaction" in text


async def run_transaction(db: Any,
                          fn: Callable[[Any], Awaitable[Any]],
                          *, max_attempts: int = _MAX_OUTER_ATTEMPTS) -> Any:
    """Run ``fn(transaction)`` with jittered contention retries."""
    from google.cloud import firestore as fs_types

    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            transaction = db.transaction(max_attempts=1)
            return await fn(transaction)
        except Exception as exc:  # noqa: BLE001 — classified below
            if attempt >= max_attempts or not _is_contention(exc):
                raise
            last_exc = exc
            delay_ms = min(_MAX_DELAY_MS,
                           _BASE_DELAY_MS * (2 ** (attempt - 1)))
            delay_ms = random.uniform(delay_ms * 0.5, delay_ms * 1.5)
            logger.debug("transaction aborted (attempt %d/%d) — retrying "
                         "in %.0fms: %s", attempt, max_attempts, delay_ms,
                         str(exc)[:120])
            await asyncio.sleep(delay_ms / 1000.0)
    raise last_exc  # pragma: no cover — unreachable (raise above)
