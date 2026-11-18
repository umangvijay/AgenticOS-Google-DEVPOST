"""Capacity service (Phase 3 §3.11/§3.16/§3.17).

Acquire → renew → release lifecycle over shared capacity leases, with the
per-tenant active-unit limits that implement fairness (§3.16: a tenant
cannot monopolize execution capacity) and a per-process semaphore so each
MACHINE stays protected even when the shared store is degraded (§3.11 #6).

Execution ordering (§3.17), implemented by `capacity_slot`:
    1. Acquire execution capacity FIRST (a queued workflow must not hold a
       browser slot while waiting for unrelated work).
    2. THEN obtain the Phase 2 spending reservation (budget_service), which
       happens inside the wrapped call body.
    3. On ANY outcome — success, budget refusal, failure, cancellation —
       capacity is released in `finally`. A budget refusal therefore cannot
       strand a capacity slot, and a capacity refusal never starts paid work.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import threading
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Dict, Optional

from backend.config.settings import settings
from backend.models.admission import (
    CapacityError,
    CapacityExhaustedError,
    CapacityLease,
    ResourceClass,
    epoch_now,
)
from backend.repositories.capacity_repository import (
    DEFAULT_TTL_SECONDS,
    get_capacity_repo,
)

logger = logging.getLogger(__name__)

# §4.10 (R12): worker identity must survive container PID reuse — an
# instance nonce (boot time) plus hostname plus pid. Recorded durably with
# every attempt via the lease's worker_id.
_WORKER_NONCE = f"{time.time_ns():x}"


def compute_worker_id() -> str:
    import socket
    return f"worker-{socket.gethostname()}-{os.getpid()}-{_WORKER_NONCE}"


WORKER_ID = compute_worker_id()


def tenant_limit(resource_class: ResourceClass) -> int:
    """Per-tenant active-unit cap for a resource class (§3.16 #1)."""
    return {
        ResourceClass.WORKFLOW_TASK: settings.TENANT_MAX_ACTIVE_TASKS,
        ResourceClass.MCP_BUILD: settings.TENANT_MAX_ACTIVE_BUILDS,
        ResourceClass.BROWSER_SESSION: settings.TENANT_MAX_ACTIVE_BROWSERS,
        ResourceClass.PROVIDER_CALL: settings.TENANT_MAX_ACTIVE_TASKS,
        ResourceClass.ARTIFACT_GENERATION: settings.TENANT_MAX_ACTIVE_TASKS,
    }[resource_class]


# ── Observability (§3.20) ──────────────────────────────────────────────

class _Metrics:
    def __init__(self) -> None:
        self.counters: Dict[str, int] = {}
        self._local_active: Dict[str, int] = {}

    def incr(self, name: str, labels: Optional[Dict[str, str]] = None,
             amount: int = 1) -> None:
        key = name
        if labels:
            flat = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if v is not None)
            key = f"{name}{{{flat}}}"
        self.counters[key] = self.counters.get(key, 0) + amount

    def note_active(self, resource_class: ResourceClass, delta: int) -> None:
        key = f"local_active_{resource_class.value}"
        self._local_active[key] = max(0, self._local_active.get(key, 0) + delta)


METRICS = _Metrics()


def capacity_metrics() -> Dict[str, int]:
    """Counters + per-process active slots. Alert on capacity_denied (a
    tenant hitting its cap) and leases_expired (workers dying mid-flight)."""
    out: Dict[str, int] = dict(METRICS.counters)
    out.update(METRICS._local_active)
    return out


# ── Local per-process guard (§3.11 #6) ─────────────────────────────────

_local_semaphores: Dict[str, asyncio.BoundedSemaphore] = {}
# §4.10 (R12): leases THIS process actually acquired a local permit for.
# Release frees a permit ONLY for these — a duplicate or foreign release can
# never increase local capacity (BoundedSemaphore would raise; we don't rely
# on the raise, we don't call release at all).
@dataclass
class _LocalPermit:
    semaphore: asyncio.BoundedSemaphore
    loop: asyncio.AbstractEventLoop
    acquired: bool = False
    lease: Optional[CapacityLease] = None
    identity: Optional[tuple] = None


_LOCALLY_ACQUIRED: dict[tuple, _LocalPermit] = {}
_LOCAL_LOCK = threading.Lock()


def _local_limit(resource_class: ResourceClass) -> int:
    # A single worker process must not run more concurrent work than its
    # configured concurrency, regardless of what the shared cap allows.
    return {
        ResourceClass.WORKFLOW_TASK: settings.LOCAL_WORKER_CONCURRENCY,
        ResourceClass.MCP_BUILD: settings.TENANT_MAX_ACTIVE_BUILDS,
        ResourceClass.BROWSER_SESSION: settings.TENANT_MAX_ACTIVE_BROWSERS,
        ResourceClass.PROVIDER_CALL: settings.LOCAL_WORKER_CONCURRENCY,
        ResourceClass.ARTIFACT_GENERATION: settings.LOCAL_WORKER_CONCURRENCY,
    }[resource_class]


def _local_semaphore(resource_class: ResourceClass) -> asyncio.BoundedSemaphore:
    key = resource_class.value
    loop = asyncio.get_running_loop()
    with _LOCAL_LOCK:
        for marker, permit in list(_LOCALLY_ACQUIRED.items()):
            if marker[0] == key and permit.loop.is_closed():
                del _LOCALLY_ACQUIRED[marker]
                _return_permit(resource_class, permit)
        sem = _local_semaphores.get(key)
        owner = getattr(sem, "_agentos_loop", getattr(sem, "_loop", None))
        if owner is not None and owner is not loop:
            if hasattr(owner, "is_closed") and not owner.is_closed():
                # Never replace another LIVE loop's guard: that would grant
                # a second complete set of process permits.
                raise CapacityExhaustedError("Local capacity belongs to another active worker loop")
            sem = None
        if sem is None:
            sem = asyncio.BoundedSemaphore(_local_limit(resource_class))
            _local_semaphores[key] = sem
        sem._agentos_loop = loop
        return sem


def id_marker(resource_class: ResourceClass, scope_key: str, execution_key: str) -> tuple:
    return resource_class.value, scope_key, execution_key


def _lease_identity(lease: CapacityLease) -> tuple:
    return lease.lease_id, lease.worker_id, lease.generation, lease.owner_id


def _return_permit(resource_class: ResourceClass, permit: _LocalPermit) -> None:
    if not permit.acquired:
        return
    permit.acquired = False
    METRICS.note_active(resource_class, -1)
    if permit.loop.is_closed():
        return  # Never return a dead loop's permit to its replacement guard.
    if asyncio.get_running_loop() is permit.loop:
        permit.semaphore.release()
    else:
        permit.loop.call_soon_threadsafe(permit.semaphore.release)


# ── Lease lifecycle ────────────────────────────────────────────────────

async def acquire(resource_class: ResourceClass, scope_key: str, owner_id: str,
                  execution_key: str, units: int = 1,
                  ttl_seconds: int = DEFAULT_TTL_SECONDS,
                  organization_id: Optional[str] = None,
                  command_id: Optional[str] = None, task_id: Optional[str] = None,
                  attempt_id: Optional[str] = None) -> CapacityLease:
    """Acquire `units` of `resource_class` for a tenant.

    Raises CapacityExhaustedError when the tenant cap is reached — callers
    DEFER work on this error (§3.17), they do not fail it.
    When no shared store is configured (local dev), only the per-process
    semaphore guards the machine; that is the documented local-mode posture.
    """
    repo = get_capacity_repo()
    local = _local_semaphore(resource_class)
    marker = id_marker(resource_class, scope_key, execution_key)
    permit = _LocalPermit(local, asyncio.get_running_loop())
    with _LOCAL_LOCK:
        previous = _LOCALLY_ACQUIRED.get(marker)
        if previous is not None and not previous.loop.is_closed():
            raise CapacityExhaustedError("This scoped execution already holds or awaits local capacity")
        _LOCALLY_ACQUIRED[marker] = permit
    if previous is not None:
        _return_permit(resource_class, previous)
    try:
        # Same-loop, nonblocking admission. No child task can acquire a
        # permit just as its caller is cancelled by wait_for().
        if local.locked():
            METRICS.incr("capacity_denied", {"resource": resource_class.value, "layer": "local"})
            raise CapacityExhaustedError(f"Local process limit for {resource_class.value} reached")
        await local.acquire()
        permit.acquired = True
        METRICS.note_active(resource_class, 1)
        if repo is not None:
            outcome = await repo.acquire(
                resource_class, scope_key, owner_id, execution_key,
                limit=tenant_limit(resource_class), units=units,
                ttl_seconds=ttl_seconds, worker_id=WORKER_ID,
                organization_id=organization_id, command_id=command_id,
                task_id=task_id, attempt_id=attempt_id)
            if outcome.created:
                METRICS.incr("leases_acquired", {"resource": resource_class.value})
            lease = outcome.lease
        else:
            lease = CapacityLease(
                lease_id=f"local_{uuid.uuid4().hex}", resource_class=resource_class,
                scope_key=scope_key, owner_id=owner_id, execution_key=execution_key,
                worker_id=WORKER_ID, units=units, generation=1,
                acquired_at_epoch=epoch_now(), expires_at_epoch=epoch_now() + ttl_seconds,
                last_heartbeat_epoch=epoch_now(),
                organization_id=organization_id, command_id=command_id,
                task_id=task_id, attempt_id=attempt_id)
            METRICS.incr("leases_acquired", {"resource": resource_class.value})
        permit.lease = lease
        permit.identity = _lease_identity(lease)
        return lease
    except BaseException as exc:
        # CancelledError is a BaseException. An interrupted store admission
        # must return the local slot; an uncertain durable write expires by
        # its existing lease TTL rather than being blindly released by key.
        with _LOCAL_LOCK:
            if _LOCALLY_ACQUIRED.get(marker) is permit:
                del _LOCALLY_ACQUIRED[marker]
        _return_permit(resource_class, permit)
        if isinstance(exc, CapacityError) and repo is not None:
            METRICS.incr("capacity_denied", {"resource": resource_class.value, "layer": "shared"})
        raise


async def renew(lease: CapacityLease, extend_seconds: int) -> CapacityLease:
    """Heartbeat a lease. Raises LeaseConflictError when a newer generation
    exists (fenced out, §3.11 #5) — the caller must stop work and NOT commit
    its result."""
    repo = get_capacity_repo()
    if repo is None:
        lease.expires_at_epoch = epoch_now() + max(30, int(extend_seconds))
        return lease
    return await repo.renew(lease.lease_id, lease.worker_id, lease.generation,
                            extend_seconds)


async def release(lease: CapacityLease) -> bool:
    """Idempotent release; a fencing conflict returns False. Unexpected
    store failures propagate after returning this lease's local permit."""
    repo = get_capacity_repo()
    try:
        released = True
        if repo is not None and not lease.lease_id.startswith("local_"):
            released = await repo.release(lease.lease_id, lease.worker_id, lease.generation)
        METRICS.incr("leases_released", {"resource": lease.resource_class.value})
        return released
    except CapacityError as exc:
        # Expired/fenced: capacity was already reclaimed. Swallowing is the
        # documented posture — release must not mask the work's own outcome.
        METRICS.incr("leases_release_noop", {"resource": lease.resource_class.value})
        logger.warning("Lease release no-op for %s (%s)", lease.lease_id, exc)
        return False
    finally:
        marker = id_marker(lease.resource_class, lease.scope_key, lease.execution_key)
        with _LOCAL_LOCK:
            permit = _LOCALLY_ACQUIRED.get(marker)
            if permit is not None and permit.identity == _lease_identity(lease):
                del _LOCALLY_ACQUIRED[marker]
            else:
                permit = None
        if permit is not None:
            _return_permit(lease.resource_class, permit)
        # Not locally acquired (foreign/duplicate release): NO permit is
        # freed — a double release cannot increase local capacity (§4.10).


async def release_execution(resource_class: ResourceClass, scope_key: str,
                            execution_key: str) -> bool:
    """Release the ACTIVE lease for a logical execution by key (used by
    resumed sessions whose original run holds the slot), and free this
    process's local concurrency slot for it. Call from the process that
    closed the resource — the session registry is per-process, so the
    closer IS the holder. Idempotent; never raises for a missing lease."""
    marker = id_marker(resource_class, scope_key, execution_key)
    with _LOCAL_LOCK:
        permit = _LOCALLY_ACQUIRED.get(marker)
        lease = permit.lease if permit else None
    if lease is None:
        return False
    # Use the original lease fence, never the repository's unfenced
    # release-by-key operation, which could retire another worker's lease.
    return await release(lease)


async def reconcile_expired() -> int:
    """Reclaim capacity from crashed workers (worker ticker, §3.11 #8)."""
    repo = get_capacity_repo()
    if repo is None:
        return 0
    swept = await repo.reconcile_expired()
    if swept:
        METRICS.incr("leases_expired", amount=swept)
        logger.warning("Capacity reconcile: %d expired lease(s) reclaimed", swept)
    return swept


async def active_usage(resource_class: ResourceClass, scope_key: str) -> int:
    repo = get_capacity_repo()
    if repo is None:
        return 0
    return await repo.active_units(resource_class, scope_key)


@contextlib.asynccontextmanager
async def capacity_slot(resource_class: ResourceClass, scope_key: str,
                        owner_id: str, execution_key: str, units: int = 1,
                        ttl_seconds: int = DEFAULT_TTL_SECONDS,
                        organization_id: Optional[str] = None,
                        command_id: Optional[str] = None,
                        task_id: Optional[str] = None,
                        attempt_id: Optional[str] = None) -> AsyncIterator[CapacityLease]:
    """§3.17 execution envelope: capacity first, budget second, release
    ALWAYS. Usage:

        async with capacity_slot(ResourceClass.BROWSER_SESSION, ...) as lease:
            result = await do_work()   # Phase 2 reservation happens in here

    A CapacityExhaustedError propagates BEFORE the body runs, so a capacity
    refusal never starts paid execution; any exception from the body (budget
    refusal included) still releases the slot."""
    lease = await acquire(resource_class, scope_key, owner_id, execution_key,
                          units=units, ttl_seconds=ttl_seconds,
                          organization_id=organization_id, command_id=command_id,
                          task_id=task_id, attempt_id=attempt_id)
    try:
        yield lease
    finally:
        await release(lease)
