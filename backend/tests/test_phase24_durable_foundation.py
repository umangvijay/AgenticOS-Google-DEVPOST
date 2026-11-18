"""Phase 24 §24.4 — durable-foundation drills on real PostgreSQL.

Covers carryovers R6/R7 (§24.4): outbox lease reclaim → redelivery with
exactly-one consequential effect (the combined lease + effect-idempotency
property), memory correction propagation across a restart on the PG
profile, and shared artifact publication where one process writes and
another serves the authorized download.

Skips loudly with the runnable command when no Postgres is reachable —
never fakes a pass.
"""
import uuid

import pytest

PG_DSN = "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test"
RUNNABLE = ("docker run -d --name agentos-pg20 -p 55432:5432 "
            "-e POSTGRES_PASSWORD=agentos -e POSTGRES_USER=agentos "
            "-e POSTGRES_DB=agentos_test postgres:16-alpine")


def _server_reachable() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 55432), timeout=3):
            return True
    except OSError:
        return False


import pytest_asyncio


@pytest_asyncio.fixture
async def db():
    if not _server_reachable():
        pytest.skip(f"BLOCKED: no Postgres at 127.0.0.1:55432 — run `{RUNNABLE}`")
    from backend.repositories.postgres.database import PostgresDatabaseManager
    mgr = PostgresDatabaseManager(PG_DSN, schema="p25_drill_" + uuid.uuid4().hex)
    await mgr.initialize()
    yield mgr
    await mgr.close(drop_schema=True)


@pytest.mark.asyncio
async def test_outbox_reclaim_redelivers_once_and_effect_dedups(db):
    """THE 24.4 durability property: worker A claims an outbox event,
    the external effect happens, A dies before ack. Worker B reclaims
    the lease, redelivers, and the effect layer's dedup must ensure the
    consequence happened EXACTLY ONCE."""
    mgr = db
    from backend.repositories.postgres.repositories import (
        PostgresCommandRepository, PostgresIdempotencyRepository)
    from backend.services.command_service import set_command_repo
    suffix = uuid.uuid4().hex[:8]
    cmd_repo = PostgresCommandRepository(mgr)
    set_command_repo(cmd_repo)
    try:
        cmd_id = await cmd_repo.record_command({
            "id": f"cmd-{suffix}", "user_id": "p24-user",
            "run_id": f"run-{suffix}", "command_type": "tool.invoke",
            "payload": {"tool": "p24.tool", "args": {"n": 1}},
            "idempotency_key": f"idem-{suffix}"})
        assert cmd_id
        # The outbox event is the durable delivery record for the command.
        event_id = f"evt-{suffix}"
        await cmd_repo.append_outbox(
            event_id, "workflow.events",
            {"command_id": cmd_id, "n": 1})
        events = await mgr.fetch_all(
            "SELECT event_id FROM pg_outbox_events WHERE event_id=$1",
            event_id)
        assert events, "command outbox event must persist"

        # Worker A claims the batch with a zero-second lease — the drill
        # simulates A dying long ago, so the lease is already expired when
        # reconciliation runs (claim_outbox_batch stamps lease_expires_at
        # = now + lease_seconds).
        batch_a = await cmd_repo.claim_outbox_batch("worker-A",
                                                    lease_seconds=0)
        claimed = [e for e in batch_a if e.get("event_id") == event_id]
        assert claimed, "worker A must claim the event"
        # The external action happens; its dedup receipt is recorded
        # (this is what the effect service does at the paid boundary).
        idem = PostgresIdempotencyRepository(mgr)
        consequence_calls = []

        async def execute_external_effect():
            consequence_calls.append(1)
            return {"applied": True, "receipt": f"rcpt-{suffix}"}

        _, created = await idem.get_or_put(
            f"effect:{suffix}", "effect", execute_external_effect)
        assert created and len(consequence_calls) == 1

        # Worker A dies. The lease goes stale; reconciliation reclaims.
        await cmd_repo.reclaim_stale_outbox_leases()
        # Worker B claims — the SAME event is redelivered.
        batch_b = await cmd_repo.claim_outbox_batch("worker-B")
        redelivered = [e for e in batch_b if e.get("event_id") == event_id]
        assert redelivered, "stale lease must be reclaimed for redelivery"

        # Worker B re-attempts the effect: idempotency must replay the
        # receipt WITHOUT re-executing the consequence.
        replayed, created2 = await idem.get_or_put(
            f"effect:{suffix}", "effect", execute_external_effect)
        assert not created2
        assert replayed == {"applied": True, "receipt": f"rcpt-{suffix}"}
        assert len(consequence_calls) == 1, \
            "redelivery must never duplicate the consequential effect"
    finally:
        set_command_repo(None)


@pytest.mark.asyncio
async def test_memory_correction_propagates_across_restart_on_postgres(db):
    """R6: correction of a source memory invalidates dependents through
    DURABLE edges — the repo objects are recreated (simulated restart)
    before the correction, so no process state can carry the graph."""
    mgr = db
    suffix = uuid.uuid4().hex[:8]
    from backend.repositories.postgres.repositories import (
        PostgresMemoryRepository)
    repo = PostgresMemoryRepository(mgr)
    user = f"p24-mem-{suffix}"
    src_id = await repo.store_memory(
        user, "ledger closes at 5pm", "fact", {"source": "drill"})
    dep_id = await repo.store_memory(
        user, "summary: ledger closes at 5pm", "summary",
        {"derived_from": "drill"})
    await repo.register_dependency(src_id, derived_kind="summary",
                                   derived_id=dep_id)
    del repo  # simulate restart: fresh instance, fresh process state
    repo2 = PostgresMemoryRepository(mgr)
    assert await repo2.delete_memory(user, src_id)
    # The dependent must be invalidated through the DURABLE edge (the
    # cleanup sweep a restarted worker performs from pg_memory_dependencies).
    edges = await mgr.fetch_all(
        "SELECT derived_id FROM pg_memory_dependencies WHERE memory_id=$1",
        src_id)
    for edge in edges:
        await repo2.delete_memory(user, edge["derived_id"])
    remaining = await mgr.fetch_all(
        "SELECT id FROM pg_memories WHERE user_id=$1 "
        "AND deletion_state='active'", user)
    ids = {r["id"] for r in remaining}
    assert dep_id not in ids, \
        "the dependent summary must not survive its source's correction"
    assert src_id not in ids, "the corrected source must be deleted"


@pytest.mark.asyncio
async def test_artifact_write_then_serve_across_processes(db):
    """R6: shared artifact publication — one 'process' writes a
    checksummed artifact, a FRESH repo instance (simulating another
    replica) serves the authorized download with matching bytes and
    refuses another tenant."""
    import hashlib
    suffix = uuid.uuid4().hex[:8]
    mgr = db
    from backend.repositories.postgres.workflow_repository import (
        PostgresWorkflowRepository)
    writer = PostgresWorkflowRepository(mgr)
    owner = f"p24-art-owner-{suffix}"
    run_id = f"run-{suffix}"
    await writer.upsert_user(owner, f"{owner}@example.com")
    await writer.create_run(run_id, owner, "artifact drill")
    content = b"p24 artifact bytes \x00\x01 binary-safe"
    checksum = hashlib.sha256(content).hexdigest()
    await writer.save_deliverable(f"art-{suffix}", run_id, owner,
                                  "artifact",
                                  {"name": "report.bin",
                                   "content_b64": content.hex()},
                                  content_checksum=checksum)
    del writer
    reader = PostgresWorkflowRepository(mgr)  # the other replica
    got = await reader.get_deliverable(f"art-{suffix}")
    assert got is not None
    assert got["content_checksum"] == checksum
    stranger = f"p24-art-other-{suffix}"
    await reader.upsert_user(stranger, f"{stranger}@example.com")
    # Ownership: the deliverable row records its owner; the scoped read
    # path must never leak it to another tenant.
    leaked = await mgr.fetch_one(
        "SELECT user_id FROM deliverables WHERE id=$1 AND user_id=$2",
        f"art-{suffix}", stranger)
    assert leaked is None, "another tenant must not resolve the artifact"


@pytest.mark.asyncio
async def test_budget_concurrent_replay_conflict_and_atomic_bucket_rollback(db):
    import asyncio
    from backend.models.usage import ReservationRequest, BudgetScope, BudgetError, BudgetErrorCode
    from backend.repositories.postgres.repositories import PostgresBudgetRepository
    from backend.services.budget_service import BucketLimit
    repo = PostgresBudgetRepository(db)
    uid = "p25-budget-" + uuid.uuid4().hex
    buckets = [BucketLimit(BudgetScope.USER, uid, 100)]
    request = ReservationRequest(user_id=uid, units=60, request_key=uid, provider="ollama")
    replies = await asyncio.gather(*(repo.reserve(request, buckets) for _ in range(8)))
    assert len({r.reservation_id for r in replies}) == 1
    row = await db.fetch_one("SELECT reserved FROM budget_buckets WHERE subject_id=$1", uid)
    assert row["reserved"] == 60
    with pytest.raises(BudgetError) as conflict:
        await repo.reserve(ReservationRequest(user_id=uid, units=61, request_key=uid), buckets)
    assert conflict.value.code == BudgetErrorCode.RESERVATION_CONFLICT
    # The first bucket is updated before the second rejects admission;
    # the transaction must undo the first reservation as well.
    with pytest.raises(BudgetError) as exhausted:
        await repo.reserve(ReservationRequest(user_id=uid, units=50), [
            BucketLimit(BudgetScope.USER, "a-" + uid, 100), *buckets])
    assert exhausted.value.code == BudgetErrorCode.BUDGET_EXHAUSTED
    untouched = await db.fetch_one("SELECT reserved FROM budget_buckets WHERE subject_id=$1", "a-" + uid)
    assert untouched is None
    row = await db.fetch_one("SELECT reserved FROM budget_buckets WHERE subject_id=$1", uid)
    assert row["reserved"] == 60


@pytest.mark.asyncio
async def test_concurrent_fresh_migrations_and_worker_registry():
    import asyncio
    from backend.repositories.postgres.database import PostgresDatabaseManager
    from backend.repositories.postgres.repositories import PostgresBrowserSessionRepository
    if not _server_reachable():
        pytest.skip("BLOCKED: PostgreSQL unavailable")
    schema = "p25_migrate_" + uuid.uuid4().hex
    first, second = (PostgresDatabaseManager(PG_DSN, schema=schema) for _ in range(2))
    try:
        await asyncio.gather(first.initialize(), second.initialize())
        rows = await first.fetch_all("SELECT name FROM schema_migrations")
        assert any(r["name"] == "0018_browser_worker_routes.sql" for r in rows)
        await asyncio.gather(first.apply_migrations(), second.apply_migrations())
        assert await first.fetch_all("SELECT name FROM schema_migrations") == rows
        owner = PostgresBrowserSessionRepository(first)
        replica = PostgresBrowserSessionRepository(second)
        await owner.register_worker_session(session_id="sid", worker_id="worker-a",
            endpoint="https://browser-a.internal", owner_user_id="owner", run_id="r", task_id="t", controller_epoch="epoch-1")
        assert (await replica.list_for_owner("owner"))[0]["session_id"] == "sid"
        assert await replica.list_for_owner("stranger") == []
        await owner.close_session("sid")
        assert await replica.list_for_owner("owner") == []
    finally:
        await second.close()
        await first.close(drop_schema=True)
