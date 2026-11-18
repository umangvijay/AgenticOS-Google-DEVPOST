"""Phase 32 §32.6 — PostgreSQL idempotency-ledger parity.

The deployed durable profile crashed the first time a tool call claimed
its ledger lock: PostgresIdempotencyRepository only implemented the
legacy get_or_put cache, so the base contract (atomic_insert,
commit_success, commit_uncertain, reconcile_uncertain, sweep) was
missing — AttributeError on a LIVE workflow. SQLite and Firestore
already implement the contract; PostgreSQL must match. These tests
exercise the REAL repository against a real PostgreSQL schema.
"""
import asyncio
import uuid

import pytest
import pytest_asyncio

PG_HOST = "127.0.0.1"
PG_PORT = 55432
PG_DSN = "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test"


def _server_reachable() -> bool:
    import socket
    try:
        with socket.create_connection((PG_HOST, PG_PORT), timeout=3):
            return True
    except OSError:
        return False


@pytest_asyncio.fixture
async def pgdb():
    if not _server_reachable():
        pytest.skip("BLOCKED: no Postgres at 127.0.0.1:55432 for the parity suite")
    from backend.repositories.postgres.database import PostgresDatabaseManager
    schema = f"p32ide_{uuid.uuid4().hex[:8]}"
    mgr = PostgresDatabaseManager(PG_DSN, schema=schema)
    await mgr.initialize()
    try:
        yield mgr
    finally:
        await mgr.close(drop_schema=True)


def _repo(mgr):
    from backend.repositories.postgres.repositories import (
        PostgresIdempotencyRepository)
    return PostgresIdempotencyRepository(mgr)


@pytest.mark.asyncio
async def test_atomic_insert_claims_once_and_reclaims_paused(pgdb):
    repo = _repo(pgdb)
    key = f"idem-{uuid.uuid4().hex[:12]}"
    assert await repo.atomic_insert(key, "wf-1", "t1"), "first claim wins"
    assert not await repo.atomic_insert(key, "wf-1", "t1"), \
        "a running lock cannot be re-claimed"
    # Terminal states are not reclaimable either.
    await repo.commit_success(key, '{"ok": true}')
    assert not await repo.atomic_insert(key, "wf-1", "t1")
    # Only a PAUSED entry is reclaimable (human-paused call).
    key2 = f"idem-{uuid.uuid4().hex[:12]}"
    assert await repo.atomic_insert(key2, "wf-1", "t2")
    await repo.commit_pause(key2)
    assert await repo.atomic_insert(key2, "wf-1", "t2"), \
        "a paused call is reclaimable"


@pytest.mark.asyncio
async def test_commit_paths_and_stale_quarantine(pgdb):
    repo = _repo(pgdb)
    key = f"idem-{uuid.uuid4().hex[:12]}"
    assert await repo.atomic_insert(key, "wf-2", "t1")
    await repo.commit_uncertain(key)
    entry = await repo.get_entry(key)
    assert entry["status"] == "uncertain"
    # Stale RUNNING entries quarantine to uncertain (audit F07), never failed.
    key2 = f"idem-{uuid.uuid4().hex[:12]}"
    assert await repo.atomic_insert(key2, "wf-2", "t2")
    await pgdb.execute(
        "UPDATE pg_idempotency SET updated_at = now() - interval '1 hour' "
        "WHERE key=$1", key2)
    swept = await repo.sweep_stale_running(older_than_seconds=1800)
    assert swept >= 1
    entry2 = await repo.get_entry(key2)
    assert entry2["status"] == "uncertain"
    # One-winner reconciliation: the loser raises, history is not rewritten.
    await repo.reconcile_uncertain(key, "completed", '{"result": 1}')
    assert (await repo.get_entry(key))["status"] == "completed"
    with pytest.raises(ValueError):
        await repo.reconcile_uncertain(key, "failed")
    # Explicit retry releases only failures.
    key3 = f"idem-{uuid.uuid4().hex[:12]}"
    assert await repo.atomic_insert(key3, "wf-3", "t1")
    await repo.commit_failure(key3)
    await repo.reset_failed_task("wf-3", "t1")
    assert (await repo.get_entry(key3))["status"] == "paused"


@pytest.mark.asyncio
async def test_get_or_put_legacy_cache_is_terminal_not_a_lock(pgdb):
    """Legacy cache rows must never block a ledger claim (migration 0027)."""
    repo = _repo(pgdb)
    key = f"cache-{uuid.uuid4().hex[:12]}"
    async def _factory():
        return {"v": 1}

    value, fresh = await repo.get_or_put(key, "scope", _factory)
    assert fresh and value == {"v": 1}
    # A legacy cache row must not behave like a live lock: the claim path
    # sees a terminal ('completed') entry and refuses — no crash, no steal.
    assert not await repo.atomic_insert(key, "wf-9", "t9")
    record = await repo.get_record(key)
    assert record["status"] == "completed"
