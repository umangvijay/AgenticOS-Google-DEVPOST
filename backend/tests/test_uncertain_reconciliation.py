"""Audit F07 acceptance: no blind replay of irreversible actions.

- A worker crash between claim and commit leaves a 'running' entry; the
  sweep quarantines it as 'uncertain' (never 'failed') and leaves fresh
  entries alone.
- An 'uncertain' entry refuses replay with an explicit reconciliation
  message.
- A deliberate reconciliation (operator/provider receipt) resolves it to
  completed (replayable) or failed (retry-with-new-key), and refuses to
  rewrite anything that is not 'uncertain'.
"""
import pytest
import pytest_asyncio

from backend.engine.idempotency_guard import IdempotencyGuard
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.idempotency_repository import SQLiteIdempotencyRepository


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "f07.db"))
    await db.initialize()
    repo = SQLiteIdempotencyRepository(db)
    yield {"db": db, "repo": repo, "guard": IdempotencyGuard(repo)}
    await db.close()


async def _backdate(env, key: str, seconds: int):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    conn = await env["db"].connection()
    await conn.execute("UPDATE idempotency_ledger SET updated_at = ? WHERE idempotency_key = ?", (old, key))
    await conn.commit()


ARGS = {"to": "x"}


def _key() -> str:
    return IdempotencyGuard.compute_key("wf-1", "t-1", "core.email", ARGS)


async def _claim(env, key: str):
    assert await env["repo"].atomic_insert(key, "wf-1", "t-1") is True


@pytest.mark.asyncio
async def test_f07_crash_orphaned_running_is_swept_to_uncertain(env):
    key = _key()
    await _claim(env, key)
    await _backdate(env, key, seconds=3600)  # worker died an hour ago

    swept = await env["repo"].sweep_stale_running(older_than_seconds=1800)

    assert swept == 1
    record = await env["repo"].get_record(key)
    assert record["status"] == "uncertain"  # NOT failed — outcome unknown


@pytest.mark.asyncio
async def test_f07_fresh_running_entries_are_not_swept(env):
    key = _key()
    await _claim(env, key)  # just claimed, worker is alive
    swept = await env["repo"].sweep_stale_running(older_than_seconds=1800)
    assert swept == 0
    assert (await env["repo"].get_record(key))["status"] == "running"


@pytest.mark.asyncio
async def test_f07_uncertain_replay_is_blocked_with_explicit_message(env):
    key = _key()
    await _claim(env, key)
    await _backdate(env, key, seconds=3600)
    await env["repo"].sweep_stale_running(older_than_seconds=1800)

    async def must_not_run():
        raise AssertionError("the guarded action must not execute again")

    result = await env["guard"].execute_once("wf-1", "t-1", "core.email", {"to": "x"}, must_not_run)
    assert result.executed is False
    assert result.blocked is True
    assert "reconcile" in result.reason.lower()


@pytest.mark.asyncio
async def test_f07_reconcile_to_completed_makes_replay_safe(env):
    key = _key()
    await _claim(env, key)
    await _backdate(env, key, seconds=3600)
    await env["repo"].sweep_stale_running(older_than_seconds=1800)

    # The provider receipt says the email WAS sent → reconcile to completed.
    await env["repo"].reconcile_uncertain(key, "completed", result_payload='{"sent": true}')

    result = await env["guard"].execute_once(
        "wf-1", "t-1", "core.email", {"to": "x"},
        lambda: (_ for _ in ()).throw(AssertionError("must not re-execute")),
    )
    assert result.from_cache is True
    assert result.result == {"sent": True}


@pytest.mark.asyncio
async def test_f07_reconcile_to_failed_keeps_blind_retry_blocked(env):
    key = _key()
    await _claim(env, key)
    await _backdate(env, key, seconds=3600)
    await env["repo"].sweep_stale_running(older_than_seconds=1800)

    # The provider receipt says the action did NOT happen → failed. A retry
    # then needs a fresh key (the workflow Retry action), not a silent replay.
    await env["repo"].reconcile_uncertain(key, "failed")

    async def must_not_run():
        raise AssertionError("failed entries are not replayable")

    result = await env["guard"].execute_once("wf-1", "t-1", "core.email", {"to": "x"}, must_not_run)
    assert result.blocked is True
    assert "Previous execution failed" in result.reason


@pytest.mark.asyncio
async def test_f07_reconcile_refuses_non_uncertain_entries(env):
    key = _key()
    await _claim(env, key)  # status = running
    with pytest.raises(ValueError, match="uncertain"):
        await env["repo"].reconcile_uncertain(key, "completed")
    with pytest.raises(ValueError):
        await env["repo"].reconcile_uncertain(key, "exploded")  # invalid outcome
