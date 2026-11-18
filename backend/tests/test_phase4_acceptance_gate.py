"""Phase 4 §4.0D acceptance gate: the three reproduced defects as failing
regression tests BEFORE implementation, passing afterward.

1. Queue over-admission (R7) — count-then-insert outside one owned
   transaction lets two independent connections through a limit of one.
2. Cross-user deduplication (R8) — globally scoped idempotency keys hand one
   user's command identity to another; payload digests are never compared.
3. Wrong-worker release (R11) — a duplicate acquire returns another
   worker's lease as permission, and that worker can release it.

Each test uses INDEPENDENT connections where the guarantee concerns multiple
replicas (§3.21 evidence rule).
"""
import asyncio

import pytest
import pytest_asyncio

from backend.models.admission import (
    CapacityError,
    LeaseConflictError,
    ResourceClass,
)


@pytest_asyncio.fixture
async def db_a(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "gate.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def db_b(tmp_path):
    """A SECOND connection to the same store — an independent replica."""
    from backend.repositories.sqlite.database import DatabaseManager
    path = None
    # reuse the same file as db_a by construction order in each test
    yield path


@pytest.mark.asyncio
async def test_gate_r7_queue_over_admission(tmp_path):
    """Two independent connections race for a limit of ONE queue slot:
    exactly ONE may be accepted. (R7: count-then-insert outside one owned
    transaction admitted both.)"""
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.admission_repository import SQLiteCommandAdmissionRepository

    path = str(tmp_path / "r7.db")
    db_a = DatabaseManager(path)
    db_b = DatabaseManager(path)
    await db_a.initialize()
    try:
        await db_b.initialize()
        repo_a = SQLiteCommandAdmissionRepository(db_a)
        repo_b = SQLiteCommandAdmissionRepository(db_b)

        # Race MANY rounds — the count-then-insert window is scheduling
        # dependent, so a single race can pass by luck. Any round admitting
        # 2 against limit 1 proves the defect.
        for round_no in range(30):
            user = f"user-{round_no}"
            results = await asyncio.gather(
                repo_a.admit_command(f"cmd-a-{round_no}", user, None, "workflow",
                                     max_pending_per_user=1, max_pending_per_org=0,
                                     max_pending_per_class=0, max_wait_seconds=3600),
                repo_b.admit_command(f"cmd-b-{round_no}", user, None, "workflow",
                                     max_pending_per_user=1, max_pending_per_org=0,
                                     max_pending_per_class=0, max_wait_seconds=3600),
            )
            accepted = sum(1 for r in results if r.accepted and not r.duplicate)
            assert accepted == 1, (
                f"round {round_no} over-admitted: "
                f"{[(r.accepted, r.duplicate) for r in results]}")
    finally:
        await db_a.close()
        await db_b.close()


@pytest.mark.asyncio
async def test_gate_r8_cross_user_idempotency_key_is_independent(tmp_path):
    """The same raw idempotency key from TWO tenants creates two INDEPENDENT
    commands. (R8: a globally scoped key handed user B user A's command.)"""
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.admission_repository import SQLiteCommandAdmissionRepository

    path = str(tmp_path / "r8.db")
    db = DatabaseManager(path)
    await db.initialize()
    try:
        repo = SQLiteCommandAdmissionRepository(db)
        first = await repo.admit_command("cmd-a", "user-a", None, "workflow",
                                         max_pending_per_user=5,
                                         max_pending_per_org=0,
                                         max_pending_per_class=0,
                                         max_wait_seconds=3600,
                                         idempotency_key="shared-key",
                                         request_digest="digest-a")
        second = await repo.admit_command("cmd-b", "user-b", None, "workflow",
                                          max_pending_per_user=5,
                                          max_pending_per_org=0,
                                          max_pending_per_class=0,
                                          max_wait_seconds=3600,
                                          idempotency_key="shared-key",
                                          request_digest="digest-b")
        assert first.accepted and second.accepted
        assert not second.duplicate, "cross-tenant key must NOT dedup"
        assert second.command_id != first.command_id
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_gate_r8_same_key_different_payload_conflicts(tmp_path):
    """Same tenant, same key, DIFFERENT payload digest → explicit conflict,
    never silent reuse of the original command."""
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.admission_repository import SQLiteCommandAdmissionRepository
    from backend.models.admission import AdmissionReason

    path = str(tmp_path / "r8b.db")
    db = DatabaseManager(path)
    await db.initialize()
    try:
        repo = SQLiteCommandAdmissionRepository(db)
        first = await repo.admit_command("cmd-1", "user-1", None, "workflow",
                                         max_pending_per_user=5,
                                         max_pending_per_org=0,
                                         max_pending_per_class=0,
                                         max_wait_seconds=3600,
                                         idempotency_key="k1",
                                         request_digest="digest-1")
        assert first.accepted
        conflict = await repo.admit_command("cmd-2", "user-1", None, "workflow",
                                            max_pending_per_user=5,
                                            max_pending_per_org=0,
                                            max_pending_per_class=0,
                                            max_wait_seconds=3600,
                                            idempotency_key="k1",
                                            request_digest="digest-DIFFERENT")
        assert conflict.accepted is False
        assert conflict.reason == AdmissionReason.IDEMPOTENCY_CONFLICT
        # Same key + SAME digest → the original command returns (true replay).
        replay = await repo.admit_command("cmd-3", "user-1", None, "workflow",
                                          max_pending_per_user=5,
                                          max_pending_per_org=0,
                                          max_pending_per_class=0,
                                          max_wait_seconds=3600,
                                          idempotency_key="k1",
                                          request_digest="digest-1")
        assert replay.accepted is True and replay.duplicate is True
        assert replay.first_command_id == "cmd-1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_gate_r11_wrong_worker_cannot_take_or_release(db_a):
    """Worker B racing the SAME execution key must NOT receive worker A's
    lease as permission, and must not be able to release A's lease. (R11:
    dedup transferred ownership.)"""
    from backend.repositories.capacity_repository import SQLiteCapacityRepository

    repo = SQLiteCapacityRepository(db_a)
    first = await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:t", "u1",
                               "run1:task1:1", limit=3, worker_id="worker-A")
    assert first.created is True

    # Same execution key, DIFFERENT worker: not permission to execute.
    second = await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:t", "u1",
                                "run1:task1:1", limit=3, worker_id="worker-B")
    assert second.created is False
    assert second.outcome == "OWNED_BY_ANOTHER_WORKER"
    with pytest.raises(CapacityError):
        _ = second.lease  # a foreign lease is never handed out as a handle

    # B cannot release A's lease even by brute force with A's generation.
    lease = await repo.get_lease(first.lease.lease_id)
    with pytest.raises((LeaseConflictError, CapacityError)):
        await repo.release(lease.lease_id, "worker-B", lease.generation)
    # A still owns the capacity.
    assert (await repo.active_units(ResourceClass.WORKFLOW_TASK, "user:t")) == 1

    # A's OWN re-acquire (same worker) IS idempotent and authorized.
    again = await repo.acquire(ResourceClass.WORKFLOW_TASK, "user:t", "u1",
                               "run1:task1:1", limit=3, worker_id="worker-A")
    assert again.outcome == "ALREADY_OWNED_BY_THIS_WORKER"
    assert again.lease.lease_id == first.lease.lease_id
