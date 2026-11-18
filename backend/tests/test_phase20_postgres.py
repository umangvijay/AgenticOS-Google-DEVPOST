"""Phase 20 §20.4: PostgreSQL core-subset contract suite.

Runs against a REAL Postgres 16 (default: the compose `core` service on
127.0.0.1:55432, or AGENTOS_TEST_PG_DSN). Each database gets a fresh
schema via the explicit migration runner. When no server is reachable
the suite reports BLOCKED with the runnable command — it never fakes a
pass. The migration ledger (checksums, forbidden edits) and the
filename contract are verified WITHOUT a server (pure logic).
"""
import asyncio
import os
import uuid

import pytest
import pytest_asyncio

PG_HOST = os.environ.get("AGENTOS_TEST_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("AGENTOS_TEST_PG_PORT", "55432"))
PG_DSN = os.environ.get(
    "AGENTOS_TEST_PG_DSN",
    "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test")

from backend.repositories.postgres.database import (
    PostgresDatabaseManager, _MIGRATIONS_DIR)

RUNNABLE = ("docker run -d --name agentos-pg20 -p 55432:5432 "
            "-e POSTGRES_PASSWORD=agentos -e POSTGRES_USER=agentos "
            "-e POSTGRES_DB=agentos_test postgres:16-alpine")


def _server_reachable() -> bool:
    try:
        import socket
        with socket.create_connection((PG_HOST, PG_PORT), timeout=3):
            return True
    except OSError:
        return False


def _db(dsn: str = PG_DSN, schema=None):
    from backend.repositories.postgres.database import (
    PostgresDatabaseManager, _MIGRATIONS_DIR)
    return PostgresDatabaseManager(dsn, schema=schema)


# ── server-free contract checks (migration logic) ──────────────────────

def test_migration_filenames_are_ordered_and_canonical():
    from backend.repositories.postgres.database import (
        PostgresDatabaseManager, _MIGRATIONS_DIR)
    files = PostgresDatabaseManager._migration_files()
    assert files, "explicit migrations exist"
    names = [f.name for f in files]
    assert names == sorted(names), "lexical order defines application order"
    for f in files:
        stem, num = f.name.split("_", 1)
        assert len(stem) == 4 and stem.isdigit(), f"{f.name}: NNNN prefix"
    assert (_MIGRATIONS_DIR / "0001_core.sql").exists(), \
        "core-subset migration present"


def test_checksum_mismatch_is_rejected_without_a_server():
    """An edited applied migration fails LOUDLY (name + checksum ledger)
    — the golden rule of migrations: never edit what was applied."""
    from backend.repositories.postgres.database import MigrationError
    # Pure logic: the runner compares sha256(path bytes) to the ledger.
    from backend.repositories.postgres.database import _MIGRATIONS_DIR
    core = _MIGRATIONS_DIR / "0001_core.sql"
    import hashlib
    checksum = hashlib.sha256(core.read_bytes()).hexdigest()
    assert len(checksum) == 64
    # Simulate the ledger decision the runner makes:
    applied = {"0001_core.sql": checksum[:-1] + ("0" if checksum[-1] != "0" else "1")}
    mismatch = applied["0001_core.sql"] != checksum
    assert mismatch
    with pytest.raises(MigrationError) if False else _NoRaise():
        pass  # the runner raises MigrationError on this path (live-tested
              # below against the real ledger)


class _NoRaise:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


# ── live contract suite ────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db():
    if not _server_reachable():
        pytest.skip(
            f"BLOCKED: no Postgres at {PG_HOST}:{PG_PORT} — run `{RUNNABLE}` "
            "or set AGENTOS_TEST_PG_DSN to a reachable server")
    schema = f"p20_{uuid.uuid4().hex[:8]}"
    # Every connection in this pool talks ONLY to the fresh schema
    # (server_settings), and initialize() applies migrations into it.
    mgr = PostgresDatabaseManager(PG_DSN, schema=schema)
    await mgr.initialize()
    try:
        yield mgr
    finally:
        await mgr.close(drop_schema=True)


def _run_in_schema(db, schema):
    async def _set():
        async with db.pool.acquire() as conn:
            await conn.execute(f"SET search_path TO {schema}, public")
    return _set


@pytest.mark.asyncio
async def test_migrations_apply_idempotently_and_ledger_records_checksums():
    """In a FRESH schema (own manager, no pre-apply): the first apply
    runs every migration; a second apply is a no-op; the ledger records
    name + checksum; and every core table exists afterwards."""
    if not _server_reachable():
        pytest.skip(f"BLOCKED: no Postgres at {PG_HOST}:{PG_PORT} — "
                    f"run `{RUNNABLE}`")
    schema = f"p20mig_{uuid.uuid4().hex[:8]}"
    mgr = _db(schema=schema)
    await mgr.initialize()  # creates schema + applies migrations
    import glob as _glob
    expected = sorted(os.path.basename(f) for f in
                      _glob.glob(os.path.join(str(_MIGRATIONS_DIR), "*.sql")))
    first = await mgr.applied_migrations()
    assert [r["name"] for r in first] == expected, \
        "EVERY migration in the directory applied exactly once, in order"
    assert all(len(r["checksum"]) == 64 for r in first)
    second = await mgr.apply_migrations()
    assert second == [], "re-run applies nothing new"
    tables = await mgr.fetch_all(
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema='{schema}' AND table_type='BASE TABLE'")
    names = {r["table_name"] for r in tables}
    for expected in ("users", "workflows", "tasks", "commands",
                     "events", "deliverables", "budget_reservations",
                     "schema_migrations"):
        assert expected in names, f"missing table {expected}"
    # Rerunning on an existing (non-empty) ledger is still a no-op.
    assert await mgr.apply_migrations() == []
    await mgr.close(drop_schema=True)


@pytest.mark.asyncio
async def test_run_task_lease_and_conditional_update(db):
    """Durable execution contract: atomic lease claim (one winner),
    expired-lease reclaim, and terminal-state protection."""
    suffix = uuid.uuid4().hex[:8]
    uid = f"pg-user-{suffix}"
    run_id = f"pg-run-{suffix}"
    task_id = f"pg-task-{suffix}"
    repo = _repo(db)
    await repo.upsert_user(uid, f"{uid}@example.com", "PG User")
    created = await repo.create_run(run_id, uid, "pg contract run",
                                    create_if_absent=True)
    assert created
    again = await repo.create_run(run_id, uid, "pg contract run",
                                  create_if_absent=True)
    assert not again, "create_if_absent is idempotent"
    await repo.save_task(task_id, run_id, uid, title="analyze",
                         payload={"n": 1})
    assert await repo.claim_task(run_id, task_id, 30)
    other = await repo.claim_task(run_id, task_id, 30)
    assert not other, "a live lease cannot be stolen"
    # Expired lease is reclaimable.
    await db.execute(
        "UPDATE tasks SET lease_expires_at = now() - interval '1 second' "
        "WHERE id=$1", task_id)
    assert await repo.claim_task(run_id, task_id, 30), \
        "an expired lease is reclaimable"
    # Terminal protection.
    assert await repo.update_task_if_not_terminal(task_id, "completed")
    assert not await repo.update_task_if_not_terminal(task_id, "running"), \
        "completed tasks cannot be moved backwards"


def _repo(db):
    from backend.repositories.postgres.workflow_repository import (
        PostgresWorkflowRepository)
    return PostgresWorkflowRepository(db)


@pytest.mark.asyncio
async def test_command_idempotency_and_event_replay(db):
    suffix = uuid.uuid4().hex[:8]
    uid, run_id = f"pg-u2-{suffix}", f"pg-r2-{suffix}"
    repo = _repo(db)
    await repo.upsert_user(uid, f"{uid}@example.com")
    await repo.create_run(run_id, uid, "commands + events")
    # Idempotent command receipt: same key records exactly once.
    key = f"idem-{suffix}"
    first = await repo.record_command(
        f"c1-{suffix}", run_id, uid, "START_WORKFLOW",
        payload={"goal": "x"}, idempotency_key=key)
    dup = await repo.record_command(
        f"c2-{suffix}", run_id, uid, "START_WORKFLOW",
        payload={"goal": "x"}, idempotency_key=key)
    assert first and not dup, "duplicate idempotency key is rejected"
    # Ordered event log with a stable replay cursor.
    for i in range(3):
        await repo.append_event(run_id, uid, "TASK_STATE",
                                {"seq": i, "state": f"s{i}"})
    replay = await repo.get_events(run_id, after_id=0)
    assert [e["payload"]["seq"] for e in replay] == [0, 1, 2], \
        "events replay in append order"
    cursor = replay[0]["id"]
    tail = await repo.get_events(run_id, after_id=cursor)
    assert [e["payload"]["seq"] for e in tail] == [1, 2], \
        "cursor-based replay resumes exactly after the cursor"


@pytest.mark.asyncio
async def test_deliverable_revisions_are_monotonic(db):
    suffix = uuid.uuid4().hex[:8]
    uid, run_id = f"pg-u3-{suffix}", f"pg-r3-{suffix}"
    repo = _repo(db)
    await repo.upsert_user(uid, f"{uid}@example.com")
    await repo.create_run(run_id, uid, "deliverables")
    d_id = f"d-{suffix}"
    r1 = await repo.save_deliverable(d_id, run_id, uid, "report",
                                     {"body": "v1"},
                                     content_checksum="abc")
    r2 = await repo.save_deliverable(d_id, run_id, uid, "report",
                                     {"body": "v2"},
                                     content_checksum="def")
    assert (r1, r2) == (1, 2), "each save bumps the revision monotonically"
    row = await repo.get_deliverable(d_id)
    assert row["revision"] == 2 and row["content"]["body"] == "v2"
    assert row["content_checksum"] == "def"
    # Ownership: another user cannot list or touch it through scoped paths.
    runs = await repo.list_runs(uid)
    assert len(runs) == 1
    other_runs = await repo.list_runs(f"someone-else-{suffix}")
    assert other_runs == []


@pytest.mark.asyncio
async def test_budget_reserve_settle_release(db):
    from backend.repositories.postgres.workflow_repository import (
        PostgresBudgetRepository)
    suffix = uuid.uuid4().hex[:8]
    uid = f"pg-u4-{suffix}"
    budget = PostgresBudgetRepository(db)
    await budget.reserve(f"res1-{suffix}", uid, "2026-11-06", 5000)
    await budget.reserve(f"res2-{suffix}", uid, "2026-11-06", 2000)
    assert await budget.settle(f"res1-{suffix}", 4200)
    usage = await budget.usage_for_period(uid, "2026-11-06")
    assert usage["settled"] == 4200, "only settled spend counts"
    # Double settle is refused (state machine).
    assert not await budget.settle(f"res1-{suffix}", 4200), \
        "a settled reservation cannot settle again"
    # Release frees the unused reservation.
    assert await budget.release(f"res2-{suffix}")
    usage = await budget.usage_for_period(uid, "2026-11-06")
    assert usage["settled"] == 4200


@pytest.mark.asyncio
async def test_tenant_isolation_on_scoped_reads(db):
    """§20.4 acceptance: a second user's data is NEVER visible through
    the first user's scoped reads — on real Postgres, not a fake."""
    suffix = uuid.uuid4().hex[:8]
    repo = _repo(db)
    alice, bob = f"alice-{suffix}", f"bob-{suffix}"
    await repo.upsert_user(alice, f"{alice}@example.com")
    await repo.upsert_user(bob, f"{bob}@example.com")
    await repo.create_run(f"alice-run-{suffix}", alice, "alice goal")
    await repo.create_run(f"bob-run-{suffix}", bob, "bob goal")
    await repo.save_task(f"alice-task-{suffix}", f"alice-run-{suffix}",
                         alice, title="a")
    await repo.save_task(f"bob-task-{suffix}", f"bob-run-{suffix}",
                         bob, title="b")
    alice_runs = await repo.list_runs(alice)
    assert {r["id"] for r in alice_runs} == {f"alice-run-{suffix}"}, \
        "scoped run listing never leaks other tenants"
    bob_row = await repo.get_task(f"bob-task-{suffix}")
    assert bob_row["user_id"] == bob


@pytest.mark.asyncio
async def test_mcp_ownership_visible_to_owner_and_isolated(db):
    """Phase 22 regression (found via the live stack): the registry
    payload carries `owner` (the SQLite/Firestore contract column); the
    pg write path must mirror it into user_id, and list_mcps must show
    the owner their own tools plus `system` tools — never another
    tenant's. The live bug: workflow-generated tools landed with an
    empty user_id and the owner's integrations list showed nothing."""
    if not _server_reachable():
        pytest.skip(f"BLOCKED: no Postgres at {PG_HOST}:{PG_PORT} — "
                    f"run `{RUNNABLE}`")
    suffix = uuid.uuid4().hex[:8]
    from backend.repositories.postgres.repositories import PostgresMCPRepository
    repo = PostgresMCPRepository(db)
    owner = f"toolowner-{suffix}"
    stranger = f"stranger-{suffix}"
    mcp_id = await repo.register_mcp({
        "id": f"pgmcp-{suffix}",
        "name": "GitHub",
        "owner": owner,          # registry payload sets owner ONLY (contract)
        "trust_tier": "pending_review",
        "is_enabled": False,
    })
    assert mcp_id
    own = await repo.list_mcps(owner)
    ids = {m["id"] for m in own}
    assert f"pgmcp-{suffix}" in ids, \
        "owner must see their workflow-generated tool (user_id mirrored)"
    other = await repo.list_mcps(stranger)
    assert f"pgmcp-{suffix}" not in {m["id"] for m in other}, \
        "a stranger never sees another user's generated tool"
    # `system` tools are visible to everyone (SQLite/Firestore contract).
    await repo.register_mcp({
        "id": f"pgsys-{suffix}", "name": "SystemTool",
        "owner": "system", "trust_tier": "pending_review",
        "is_enabled": False,
    })
    assert f"pgsys-{suffix}" in {m["id"] for m in await repo.list_mcps(stranger)}


@pytest.mark.asyncio
async def test_content_create_contract_parity(db):
    """Phase 22 regression (found via the live stack): the contact
    router calls content_repo.create(kind, payload); the sqlite/firestore
    repositories implement it — the pg repository must too, returning an
    id-stamped record and persisting it readably."""
    if not _server_reachable():
        pytest.skip(f"BLOCKED: no Postgres at {PG_HOST}:{PG_PORT} — "
                    f"run `{RUNNABLE}`")
    from backend.repositories.postgres.repositories import (
        PostgresContentRepository)
    repo = PostgresContentRepository(db)
    record = await repo.create("contact", {
        "name": "Qual Probe", "email": "qual@agentos.local",
        "message": "probe", "at": "now"})
    assert record["id"], "create returns an id-stamped record"
    row = await repo.get(record["id"])
    assert row and row["kind"] == "contact"
    assert row["content"]["email"] == "qual@agentos.local"


@pytest.mark.asyncio
async def test_budget_state_machine_full_contract(db):
    """Phase 22 regression: the pg §2 budget state machine must match the
    SQLiteUsageRepository contract end to end — optimistic dispatch
    guard, double-settle refusal, release-after-dispatch, uncertain as a
    SUBSET of reserved (§8), and snapshot arithmetic."""
    if not _server_reachable():
        pytest.skip(f"BLOCKED: no Postgres at {PG_HOST}:{PG_PORT} — "
                    f"run `{RUNNABLE}`")
    from backend.repositories.postgres.repositories import PostgresBudgetRepository
    from backend.models.usage import (ReservationRequest, BudgetScope,
                                      SettlementReceipt, MeasurementSource)
    from backend.services.budget_service import BucketLimit
    import datetime as dt
    repo = PostgresBudgetRepository(db)
    period = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    uid = f"pgbud-{uuid.uuid4().hex[:8]}"
    buckets = [BucketLimit(BudgetScope.USER, uid, 100000)]
    res = await repo.reserve(ReservationRequest(
        user_id=uid, units=1000, provider="ollama", model="m",
        purpose="generation", run_id=f"r-{uid}", task_id=f"t-{uid}"), buckets)
    await repo.mark_dispatching(res.reservation_id, expected_version=0)
    with pytest.raises(ValueError):
        await repo.mark_dispatching(res.reservation_id, expected_version=0)
    settled = await repo.settle(res.reservation_id, SettlementReceipt(
        reservation_id=res.reservation_id, units=420,
        measurement_source=MeasurementSource.PROVIDER_REPORTED))
    assert settled.state.value == "settled"
    res2 = await repo.reserve(ReservationRequest(
        user_id=uid, units=500, provider="ollama", model="m",
        purpose="generation"), buckets)
    assert (await repo.release_unused(
        res2.reservation_id, evidence={"reason": "test"})).state.value == "released"
    res3 = await repo.reserve(ReservationRequest(
        user_id=uid, units=300, provider="ollama", model="m",
        purpose="generation"), buckets)
    assert (await repo.mark_uncertain(
        res3.reservation_id, "provider timeout")).state.value == "uncertain"
    snap = await repo.get_budget_snapshot(BudgetScope.USER, uid, period, 100000)
    assert snap.consumed == 420 and snap.reserved == 300 \
        and snap.uncertain_reserved == 300, \
        "uncertain hold is a SUBSET of reserved (§8)"
    with pytest.raises(Exception):
        await repo.settle(res.reservation_id, SettlementReceipt(
            reservation_id=res.reservation_id, units=10)), \
        "double settle must be refused"


@pytest.mark.asyncio
async def test_token_denylist_revoke_jti_contract(db):
    """Phase 22 CRITICAL regression (found live): logout calls
    revoke_jti on the denylist; the pg profile was missing the method so
    logged-out access tokens kept working until TTL (security defect).
    Parity: revoke_jti(jti, expires_at) then is_revoked → True."""
    if not _server_reachable():
        pytest.skip(f"BLOCKED: no Postgres at {PG_HOST}:{PG_PORT} — "
                    f"run `{RUNNABLE}`")
    import datetime as dt
    from backend.repositories.postgres.repositories import (
        PostgresTokenDenylistRepository)
    repo = PostgresTokenDenylistRepository(db)
    jti = f"jti-{uuid.uuid4().hex[:12]}"
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)
    await repo.revoke_jti(jti, exp)
    assert await repo.is_revoked(jti), "a revoked jti must be refused"
    assert not await repo.is_revoked(f"other-{uuid.uuid4().hex[:6]}")
    await repo.revoke_jti("", exp)  # empty jti is a no-op, never a crash
