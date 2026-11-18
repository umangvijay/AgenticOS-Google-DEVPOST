"""Admission service (Phase 3 §3.12/§3.17).

The ONE coordinator a protected route or worker path calls to accept new
logical work. Execution ordering (§3.17):

    1. authorization          — the route's dependency (Phase 1) — done before
    2. request admission      — policy rate limits (security/rate_limiter)
    3. command admission      — THIS service: durable pending-command record
    4. persist accepted work  — the route persists the run/build
    5. capacity + budget      — execution time (engine + budget_service)

Distinguishing HTTP request rate from LOGICAL command admission (§3.10): a
client retrying the same command gets the EXISTING admission back
(duplicate=True) instead of a second queue slot; repeated client attempts
that ARE new commands count against the tenant's pending caps.
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.config.settings import settings
from backend.models.admission import AdmissionReason
from backend.repositories.admission_repository import (
    CommandAdmission,
    get_command_admission_repo,
)

logger = logging.getLogger(__name__)


class _Metrics:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    def incr(self, name: str, labels: Optional[dict[str, str]] = None) -> None:
        key = name
        if labels:
            flat = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if v is not None)
            key = f"{name}{{{flat}}}"
        self.counters[key] = self.counters.get(key, 0) + 1


METRICS = _Metrics()


def admission_service_metrics() -> dict:
    return {"counters": dict(METRICS.counters)}


async def admit_command(command_id: str, user_id: str, org_id: Optional[str],
                        workload_class: str, idempotency_key: Optional[str] = None,
                        max_pending_per_user: Optional[int] = None,
                        max_pending_per_org: Optional[int] = None,
                        max_pending_per_class: Optional[int] = None,
                        max_wait_seconds: int = 2 * 3600,
                        request_digest: Optional[str] = None) -> CommandAdmission:
    """Transactionally admit one logical command into the bounded queue.

    - QUEUE_FULL → the tenant's pending cap is reached; caller returns 429
      with the structured body (never a success that implies future run).
    - duplicate=True → the command was already accepted; the caller returns
      the original identifier instead of enqueueing again.
    - No repository configured (local dev) → admit unconditionally; the
      documented local-mode posture is unbounded in-memory transport.
    """
    repo = get_command_admission_repo()
    if repo is None:
        METRICS.incr("commands_admitted", {"class": workload_class, "mode": "local"})
        return CommandAdmission(True, command_id)
    admission = await repo.admit_command(
        command_id, user_id, org_id, workload_class,
        max_pending_per_user=max_pending_per_user or settings.TENANT_MAX_PENDING_COMMANDS,
        max_pending_per_org=max_pending_per_org or (settings.TENANT_MAX_PENDING_COMMANDS * 4),
        max_pending_per_class=max_pending_per_class or (settings.TENANT_MAX_PENDING_COMMANDS * 8),
        max_wait_seconds=max_wait_seconds,
        idempotency_key=idempotency_key,
        request_digest=request_digest,
    )
    if admission.duplicate:
        METRICS.incr("commands_duplicate", {"class": workload_class})
    elif admission.accepted:
        METRICS.incr("commands_admitted", {"class": workload_class})
    else:
        METRICS.incr("commands_rejected",
                     {"class": workload_class, "reason": admission.reason.value})
        logger.warning("Command admission rejected (%s) for user %s class %s",
                       admission.reason.value, user_id, workload_class)
    return admission


async def mark_dispatched(command_id: str) -> None:
    repo = get_command_admission_repo()
    if repo is not None:
        await repo.mark_dispatched(command_id)


async def settle_command(command_id: str) -> None:
    """The command reached a terminal state — free its queue slot."""
    repo = get_command_admission_repo()
    if repo is not None:
        await repo.settle_command(command_id)
        METRICS.incr("commands_settled")


async def expire_stale() -> int:
    repo = get_command_admission_repo()
    if repo is None:
        return 0
    return await repo.expire_stale()


async def queue_stats() -> dict:
    """§3.20: per-class pending counts + oldest wait. Empty when no
    repository is configured (local mode keeps no durable queue)."""
    repo = get_command_admission_repo()
    if repo is None:
        return {}
    try:
        return await repo.pending_stats()
    except Exception:
        logger.warning("pending_stats failed", exc_info=True)
        return {}
