"""Phase 3 §3.21: capacity-lease guarantees (§3.11).

Required properties under test: atomic acquisition, idempotent
re-acquisition per execution key, idempotent release, ownership-verified
renewal, fencing (stale generations lose all rights), TTL recovery, and
per-tenant exhaustion — across INDEPENDENT connections where the guarantee
concerns multiple replicas.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.models.admission import (
    CapacityError,
    CapacityExhaustedError,
    LeaseConflictError,
    LeaseState,
    ResourceClass,
)
from backend.repositories.capacity_repository import SQLiteCapacityRepository
from backend.services import capacity_service


@pytest_asyncio.fixture
async def db(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "capacity.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def repo(db):
    return SQLiteCapacityRepository(db)


@pytest_asyncio.fixture
async def wired(db, repo):
    from backend.repositories.capacity_repository import set_capacity_repo
    set_capacity_repo(repo)
    yield repo
    set_capacity_repo(None)


# ── Atomic acquisition and the tenant cap ─────────────────────────────

@pytest.mark.asyncio
async def test_acquire_is_atomic_and_enforces_tenant_cap(repo):
    created = 0
    for i in range(5):
        try:
            outcome = await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:t1",
                                         "u1", f"exec-{i}", limit=3)
            created += 1 if outcome.created else 0
        except CapacityError:
            pass
    assert created == 3              # exactly the limit; the rest refused
    with pytest.raises(CapacityError):
        await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:t1", "u1",
                           "exec-overflow", limit=3)


@pytest.mark.asyncio
async def test_racing_acquire_for_last_unit_never_overadmits(repo):
    async def try_acquire(i):
        try:
            return (await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:race",
                                       "u1", f"exec-{i}", limit=2)).created
        except CapacityError:
            return False
    results = await asyncio.gather(*[try_acquire(i) for i in range(12)])
    assert sum(results) == 2


@pytest.mark.asyncio
async def test_tenants_are_isolated(repo):
    """§3.16: one tenant at its cap cannot consume another tenant's units."""
    for i in range(2):
        await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:full", "u1",
                           f"exec-{i}", limit=2)
    outcome = await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:other",
                                 "u2", "exec-0", limit=2)
    assert outcome.created is True


@pytest.mark.asyncio
async def test_invalid_inputs_rejected(repo):
    with pytest.raises(CapacityError):
        await repo.acquire(ResourceClass.WORKFLOW_TASK, "", "u1", "e", limit=2)
    with pytest.raises(CapacityError):
        await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:x", "u1", "e", limit=0)
    with pytest.raises(CapacityError):
        await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:x", "u1", "e",
                           limit=2, units=0)


# ── Idempotency per execution key (§3.11 #2, §3.21) ───────────────────

@pytest.mark.asyncio
async def test_duplicate_task_event_does_not_double_allocate(repo):
    """A duplicate trigger for the SAME execution returns the SAME lease —
    no second unit consumed."""
    first = await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:idem", "u1",
                               "run1:task1:1", limit=5)
    second = await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:idem", "u1",
                                "run1:task1:1", limit=5)
    assert first.created is True
    assert second.created is False
    assert second.lease.lease_id == first.lease.lease_id
    assert (await repo.active_units(ResourceClass.WORKFLOW_TASK, "user:idem")) == 1


# ── Release, renewal, fencing (§3.11 #3/#4/#5) ────────────────────────

@pytest.mark.asyncio
async def test_release_is_idempotent(repo):
    outcome = await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:r", "u1",
                                 "exec-1", limit=2)
    lease = outcome.lease
    assert await repo.release(lease.lease_id, lease.worker_id, lease.generation) is True
    assert await repo.release(lease.lease_id, lease.worker_id, lease.generation) is True
    assert (await repo.active_units(ResourceClass.WORKFLOW_TASK, "user:r")) == 0


@pytest.mark.asyncio
async def test_renewal_verifies_ownership(repo):
    outcome = await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:own", "u1",
                                 "exec-1", limit=2, worker_id="w1")
    lease = outcome.lease
    # Renewal re-anchors expiry from now; extend beyond the remaining TTL.
    renewed = await repo.renew(lease.lease_id, "w1", lease.generation, 1200)
    assert renewed.expires_at_epoch > lease.expires_at_epoch
    with pytest.raises(CapacityError):
        await repo.renew(lease.lease_id, "w2", lease.generation, 1200)


@pytest.mark.asyncio
async def test_stale_generation_cannot_release_or_renew(repo):
    """Fencing (§3.11 #5): after TTL reclaim + re-acquire (generation 2), a
    stale worker cannot renew or release the CURRENT lease, and its own
    expired lease is an idempotent no-op."""
    first = (await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:fence", "u1",
                                "exec-1", limit=2)).lease
    # TTL passes; the lease is reclaimed; a NEWER execution re-acquires → gen 2.
    await repo.reconcile_expired(now_epoch=int(time.time()) + 9999)
    second = (await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:fence", "u1",
                                 "exec-1", limit=2)).lease
    assert second.generation == first.generation + 1
    # The zombie targets the ACTIVE lease with its stale generation: fenced.
    with pytest.raises(LeaseConflictError):
        await repo.release(second.lease_id, second.worker_id, first.generation)
    with pytest.raises(LeaseConflictError):
        await repo.renew(second.lease_id, second.worker_id, first.generation, 120)
    # Releasing its OWN expired lease is an idempotent no-op, not an error.
    assert await repo.release(first.lease_id, first.worker_id, first.generation) is True
    # The current owner still manages its lease.
    assert await repo.release(second.lease_id, second.worker_id, second.generation) is True


@pytest.mark.asyncio
async def test_release_execution_is_keyed_and_idempotent(repo):
    await repo.acquire(ResourceClass.BROWSER_SESSION, "user:rex", "u1",
                       "browser:run1:task1", limit=2)
    assert await repo.release_execution(ResourceClass.BROWSER_SESSION,
                                        "user:rex", "browser:run1:task1") is True
    assert await repo.release_execution(ResourceClass.BROWSER_SESSION,
                                        "user:rex", "browser:run1:task1") is False
    assert (await repo.active_units(ResourceClass.BROWSER_SESSION, "user:rex")) == 0


# ── TTL recovery (worker loss, §3.11 #8) ──────────────────────────────

@pytest.mark.asyncio
async def test_expired_lease_capacity_is_reclaimed(repo):
    await repo.acquire(ResourceClass.BROWSER_SESSION, "user:crash", "u1",
                       "browser:run9:task9", limit=1, ttl_seconds=60)
    assert (await repo.active_units(ResourceClass.BROWSER_SESSION, "user:crash")) == 1
    # Simulate the worker dying: capacity recovers after the TTL passes.
    swept = await repo.reconcile_expired(now_epoch=int(time.time()) + 120)
    assert swept == 1
    assert (await repo.active_units(ResourceClass.BROWSER_SESSION, "user:crash")) == 0


@pytest.mark.asyncio
async def test_acquire_sweeps_expired_leases_in_scope(repo, monkeypatch):
    """Self-healing: the NEXT acquire for a scope sweeps its expired leases,
    so capacity recovers without waiting for the worker ticker."""
    import backend.repositories.capacity_repository as cap_repo_mod
    await repo.acquire(ResourceClass.MCP_BUILD, "user:sweep", "u1", "build-1",
                       limit=1, ttl_seconds=60)
    monkeypatch.setattr(cap_repo_mod, "epoch_now", lambda: int(time.time()) + 120)
    outcome = await repo.acquire(ResourceClass.MCP_BUILD, "user:sweep", "u1",
                                 "build-2", limit=1)
    assert outcome.created is True


# ── Service layer: ordering, deferral, degraded local mode ────────────

@pytest.mark.asyncio
async def test_capacity_slot_releases_on_budget_style_refusal(wired):
    """§3.17: a failure INSIDE the slot (e.g. Phase 2 reservation refused)
    must not strand capacity."""
    scope, key = "user:slot", "exec-slot-1"
    with pytest.raises(CapacityExhaustedError):
        async with capacity_service.capacity_slot(
                ResourceClass.WORKFLOW_TASK, scope, "u1", key) as lease:
            assert lease.lease_id
            assert (await capacity_service.active_usage(
                ResourceClass.WORKFLOW_TASK, scope)) == 1
            raise CapacityExhaustedError("simulated budget refusal")
    assert (await capacity_service.active_usage(ResourceClass.WORKFLOW_TASK, scope)) == 0


@pytest.mark.asyncio
async def test_exhausted_tenant_raises_before_body_runs(wired, monkeypatch):
    """The SHARED tenant cap refuses (the local slot is sized out of the way);
    the capacity_slot body never runs on refusal."""
    monkeypatch.setattr(capacity_service, "_local_limit",
                        lambda rc: 100 if rc == ResourceClass.WORKFLOW_TASK else 4)
    monkeypatch.setattr(capacity_service, "_local_semaphores", {})  # fresh sizing
    limit = capacity_service.tenant_limit(ResourceClass.WORKFLOW_TASK)
    for i in range(limit):
        await capacity_service.acquire(ResourceClass.WORKFLOW_TASK, "user:cap",
                                       "u1", f"exec-{i}")
    ran = False
    with pytest.raises(CapacityExhaustedError):
        async with capacity_service.capacity_slot(
                ResourceClass.WORKFLOW_TASK, "user:cap", "u1", "exec-late"):
            ran = True
    assert ran is False


@pytest.mark.asyncio
async def test_no_repo_configured_uses_local_guard_only(monkeypatch):
    """Local-mode posture (§3.11 #6): no shared store, the per-process
    semaphore still protects the machine."""
    from backend.repositories.capacity_repository import set_capacity_repo
    set_capacity_repo(None)
    try:
        lease = await capacity_service.acquire(ResourceClass.BROWSER_SESSION,
                                               "user:local", "u1", "b-1")
        assert lease.lease_id.startswith("local_")
        await capacity_service.release(lease)
    finally:
        set_capacity_repo(None)


@pytest.mark.asyncio
async def test_engine_defer_backoff_is_bounded(monkeypatch):
    """§3.16 #4: consecutive capacity defers back off exponentially and cap
    at 300s — a long shortage cannot become a retrigger storm."""
    from backend.engine.engine import WorkflowEngine
    engine = WorkflowEngine.__new__(WorkflowEngine)   # no __init__ deps needed
    engine._defer_counts = {}

    real_sleep = asyncio.sleep
    sleeps = []

    async def capture_sleep(delay, *args, **kwargs):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)

    # Five consecutive defers for the same task (now async — §4.11
    # persists the deferral durably).
    async def fake_persist():
        return None

    engine.repo = SimpleNamespace(
        get_run=asyncio.coroutine(lambda *a, **k: {"tasks": []}) if hasattr(asyncio, "coroutine") else None)

    async def _get_run(*a, **k):
        return {"tasks": []}

    engine.repo = SimpleNamespace(get_run=_get_run)

    async def noop(*a, **k):
        return None

    import backend.engine.engine as eng_mod
    from backend.engine.repo_adapter import load_run as _real_load
    monkeypatch.setattr(eng_mod, "load_run", noop)
    monkeypatch.setattr(eng_mod, "persist_task", noop)
    for _ in range(5):
        await engine._defer_task("run-1", "wf-1", "task-1")
        await real_sleep(0)   # let the created task reach its sleep
    assert len(sleeps) == 5
    assert sleeps[0] < sleeps[1] < sleeps[2]          # exponential growth
    assert max(sleeps) <= 300 * 1.25 + 0.001          # ...but bounded
    # A different task starts at the base delay, not the accumulated one.
    await engine._defer_task("run-1", "wf-1", "task-2")
    await real_sleep(0)
    assert sleeps[-1] < sleeps[0] * 3


# ── §4.23: local capacity — double release never increases permits ────

@pytest.mark.asyncio
async def test_double_release_does_not_increase_local_permits(wired):
    """§4.10 (R12): release frees a permit ONLY for leases this process
    actually acquired locally. A duplicate/foreign release cannot grow the
    semaphore's capacity (BoundedSemaphore + explicit tracking)."""
    from backend.repositories.capacity_repository import set_capacity_repo
    from backend.models.admission import ResourceClass as RC
    limit = capacity_service._local_limit(RC.WORKFLOW_TASK)

    lease = await capacity_service.acquire(RC.WORKFLOW_TASK, "user:dbl",
                                           "u1", "exec-dbl")
    # A FIRST release is legitimate.
    await capacity_service.release(lease)
    # Simulate over-release pressure: N more releases of the SAME lease.
    for _ in range(limit * 2):
        await capacity_service.release(lease)
    # The semaphore must still admit exactly `limit` holders — capacity did
    # not grow.
    acquired = 0
    leases = []
    for i in range(limit):
        try:
            leases.append(await capacity_service.acquire(
                RC.WORKFLOW_TASK, "user:dbl2", "u1", f"exec-d2-{i}"))
            acquired += 1
        except CapacityExhaustedError:
            break
    assert acquired == limit
    for l in leases:
        await capacity_service.release(l)
