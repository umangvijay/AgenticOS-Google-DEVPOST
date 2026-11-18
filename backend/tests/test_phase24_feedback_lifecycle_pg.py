"""Phase 24 §24.4 (R7) — feedback lifecycle on the POSTGRES profile.

The full feedback contract on real Postgres: submit (with consent and
run linkage) → triage transitions → user deletion → retention purge →
cross-user isolation. Complements the SQLite HTTP suite; the PostgreSQL
path was previously unexecuted.

Runnable: pytest backend/tests/test_phase24_feedback_lifecycle_pg.py -q
"""
import uuid

import pytest


def _pg_up() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 55432), timeout=3):
            return True
    except OSError:
        return False


import pytest_asyncio


@pytest_asyncio.fixture
async def mgr():
    if not _pg_up():
        pytest.skip("BLOCKED: no Postgres at 127.0.0.1:55432 — "
                    "run `docker run -d --name agentos-pg20 -p 55432:5432 "
                    "-e POSTGRES_PASSWORD=agentos -e POSTGRES_USER=agentos "
                    "-e POSTGRES_DB=agentos_test postgres:16-alpine`")
    from backend.repositories.postgres.database import PostgresDatabaseManager
    m = PostgresDatabaseManager(
        "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test",
        schema="p26_feedback_" + uuid.uuid4().hex)
    await m.initialize()
    yield m
    await m.close(drop_schema=True)


@pytest.mark.asyncio
async def test_feedback_lifecycle_submit_triage_delete_retention(mgr):
    from backend.repositories.postgres.repositories import (
        PostgresFeedbackRepository)
    repo = PostgresFeedbackRepository(mgr)
    suffix = uuid.uuid4().hex[:8]
    owner = f"p24-fb-owner-{suffix}"
    stranger = f"p24-fb-other-{suffix}"
    run_id = f"run-{suffix}"

    # 1. Submit: consent + run linkage recorded.
    fid = await repo.submit({
        "user_id": owner, "run_id": run_id,
        "helpful": False, "category": "incorrect_result",
        "expected_result": "the ledger total",
        "comment": "the total was off by one",
        "share_diagnostics": True,
        "app_version": "candidate-20261108-172759",
    })
    assert fid
    row = await repo.get(fid)
    assert row["user_id"] == owner and row["run_id"] == run_id
    assert row["category"] == "incorrect_result"
    assert row["share_diagnostics"] is True
    assert row["comment"] == "the total was off by one"

    # 2. Triage transitions: open → triaged → fixed.
    for status in ("investigating", "planned", "fixed"):
        assert await repo.set_triage(fid, status, actor="admin-q")
    assert (await repo.get(fid))["triage_status"] == "fixed"

    # 3. Cross-user isolation: a stranger cannot see or delete it.
    theirs = await repo.list_for_user(stranger)
    assert fid not in {r["id"] for r in theirs}
    assert not await repo.delete_user_feedback(stranger, fid)
    assert await repo.get(fid) is not None

    # 4. The OWNER can delete their own feedback.
    assert await repo.delete_user_feedback(owner, fid)
    assert await repo.get(fid) is None

    # 5. Retention purge: an aged row disappears, a fresh row survives.
    old_id = await repo.submit({
        "user_id": owner, "run_id": None, "helpful": True,
        "category": "other", "share_diagnostics": False,
        "app_version": "candidate-20261108-172759"})
    await mgr.execute("UPDATE pg_feedback SET created_at = now() - interval "
                      "'90 days' WHERE id=$1", old_id)
    fresh_id = await repo.submit({
        "user_id": owner, "run_id": None, "helpful": True,
        "category": "other", "share_diagnostics": False,
        "app_version": "candidate-20261108-172759"})
    purged = await repo.apply_retention(30)
    assert purged == 1
    assert await repo.get(old_id) is None, "aged feedback must be purged"
    assert await repo.get(fresh_id) is not None, \
        "fresh feedback must survive retention"
