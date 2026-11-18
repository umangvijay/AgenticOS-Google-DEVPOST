"""Firestore parity for the Phase 3 admission stores (§3.21).

Completion condition: the SAME contract tests pass against SQLite and
Firestore. These tests run the core shared-rate-limit, capacity-lease, and
command-admission matrix against the Firestore implementations whenever a
Firestore emulator is configured:

    FIRESTORE_EMULATOR_HOST=localhost:8080 \\
        GOOGLE_CLOUD_PROJECT=demo pytest backend/tests/test_phase3_firestore_parity.py

Without an emulator they are skipped (SQLite parity is covered by
test_distributed_rate_limits.py / test_capacity_leases.py /
test_queue_backpressure.py, which these tests mirror on purpose).
"""
import os
import uuid

import pytest
import pytest_asyncio

_RUN = uuid.uuid4().hex[:8]

_EMULATOR = os.environ.get("FIRESTORE_EMULATOR_HOST", "").strip()

pytestmark = pytest.mark.skipif(
    not _EMULATOR,
    reason="Firestore parity requires FIRESTORE_EMULATOR_HOST (e.g. localhost:8080)",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_emulator_data():
    """The emulator is persistent across runs — wipe the Phase 3
    collections before each test so assertions never see accumulated state."""
    from backend.repositories.firestore import database as fs_database
    fs_database.FirestoreDB._instance = None
    try:
        client = await fs_database.FirestoreDB.get_client()
        for name in ("rate_limits", "capacity_leases", "pending_commands",
                     "schedule_occurrences"):
            async for doc in client.collection(name).stream():
                await doc.reference.delete()
    finally:
        client = fs_database.FirestoreDB._instance
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            fs_database.FirestoreDB._instance = None
    yield


@pytest_asyncio.fixture
async def fs():
    from backend.config.settings import settings as app_settings
    if not app_settings.GOOGLE_CLOUD_PROJECT:
        pytest.skip("Firestore parity requires GOOGLE_CLOUD_PROJECT (any value against the emulator)")
    from backend.repositories.firestore import database as fs_database
    # pytest-asyncio gives each test its own event loop; the cached gRPC
    # client is loop-bound — every test gets a FRESH client.
    fs_database.FirestoreDB._instance = None
    try:
        yield True
    finally:
        client = fs_database.FirestoreDB._instance
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            fs_database.FirestoreDB._instance = None


@pytest.mark.asyncio
async def test_shared_rate_limit_first_record_and_zero_allowance(fs):
    """§3.5 on Firestore: first record with limit 0 is refused WITHOUT a
    document; limit 1 admits once then denies."""
    from backend.security.shared_rate_limiter import FirestoreSharedRateRepository
    repo = FirestoreSharedRateRepository()
    window = (await _current_bounds(60))[0]
    zero = await repo.admit("ip:zero-fs", "auth", window, 0, window_seconds=60)
    assert zero.allowed is False and zero.count == 0
    one = await repo.admit("ip:one-fs", "auth", window, 1, window_seconds=60)
    assert one.allowed is True and one.count == 1
    denied = await repo.admit("ip:one-fs", "auth", window, 1, window_seconds=60)
    assert denied.allowed is False


@pytest.mark.asyncio
async def test_shared_rate_limit_exhausted_window_survives_cleanup(fs):
    """§3.4 acceptance on Firestore: cleanup during an active exhausted
    window keeps enforcing."""
    from backend.security.shared_rate_limiter import FirestoreSharedRateRepository
    repo = FirestoreSharedRateRepository()
    window, _ = await _current_bounds(60)
    assert (await repo.admit("ip:busy-fs", "auth", window, 1)).allowed is True
    assert (await repo.admit("ip:busy-fs", "auth", window, 1)).allowed is False
    await repo.purge_expired(older_than_epoch=0)
    assert (await repo.admit("ip:busy-fs", "auth", window, 1)).allowed is False


@pytest.mark.asyncio
async def test_capacity_leases_atomic_and_idempotent_on_firestore(fs):
    """§3.11 on Firestore: exactly `limit` wins under contention; duplicate
    execution keys re-acquire the SAME lease; fencing rejects stale
    generations."""
    from backend.models.admission import CapacityError, LeaseConflictError, ResourceClass
    from backend.repositories.capacity_repository import FirestoreCapacityRepository
    repo = FirestoreCapacityRepository()
    scope = f"user:cap-{_RUN}"

    outcomes = []
    for i in range(5):
        try:
            outcome = await repo.acquire(ResourceClass.WORKFLOW_TASK, scope,
                                         "u1", f"exec-{i}", limit=3)
            outcomes.append(outcome)
        except CapacityError:
            pass
    assert sum(1 for o in outcomes if o.created) == 3

    # Free one unit — the idempotency section below needs room (a NEW
    # execution key at full capacity must raise, mirroring SQLite).
    freed = outcomes[0]
    await repo.release(freed.lease.lease_id, freed.lease.worker_id,
                       freed.lease.generation)

    first = await repo.acquire(ResourceClass.WORKFLOW_TASK, scope, "u1",
                               "exec-idem", limit=3)
    second = await repo.acquire(ResourceClass.WORKFLOW_TASK, scope, "u1",
                                "exec-idem", limit=3)
    assert first.created is True and second.created is False
    assert second.lease.lease_id == first.lease.lease_id

    # Fencing: a stale generation cannot release the current lease.
    newer = await repo.acquire(ResourceClass.WORKFLOW_TASK, scope, "u1",
                               f"exec-fence-{_RUN}", limit=10)
    with pytest.raises(LeaseConflictError):
        await repo.release(newer.lease.lease_id, newer.lease.worker_id,
                           newer.lease.generation + 1)


@pytest.mark.asyncio
async def test_command_admission_caps_and_duplicates_on_firestore(fs):
    """§3.12 on Firestore: tenant pending cap rejects; duplicates by command
    id and idempotency key return the existing admission; settled frees."""
    from backend.models.admission import AdmissionReason
    from backend.repositories.admission_repository import FirestoreCommandAdmissionRepository
    repo = FirestoreCommandAdmissionRepository()
    for i in range(2):
        result = await repo.admit_command(f"c-{_RUN}-{i}", f"u-{_RUN}", None,
                                          "workflow", 2, 0, 0, 3600)
        assert result.accepted
    rejected = await repo.admit_command(f"c-{_RUN}-late", f"u-{_RUN}", None,
                                        "workflow", 2, 0, 0, 3600)
    assert rejected.accepted is False
    assert rejected.reason == AdmissionReason.QUEUE_FULL

    first = await repo.admit_command(f"d1-{_RUN}", f"u2-{_RUN}", None, "build",
                                     5, 0, 0, 3600, idempotency_key=f"idem-{_RUN}")
    second = await repo.admit_command(f"d2-{_RUN}", f"u2-{_RUN}", None, "build",
                                      5, 0, 0, 3600, idempotency_key=f"idem-{_RUN}")
    assert first.accepted and second.accepted and second.duplicate
    assert second.first_command_id == f"d1-{_RUN}"

    await repo.settle_command(f"c-{_RUN}-0")
    freed = await repo.admit_command(f"c-{_RUN}-fresh", f"u-{_RUN}", None,
                                     "workflow", 2, 0, 0, 3600)
    assert freed.accepted is True


async def _current_bounds(window_seconds: int):
    import time
    now = int(time.time())
    start = now - (now % window_seconds)
    return start, start + window_seconds
