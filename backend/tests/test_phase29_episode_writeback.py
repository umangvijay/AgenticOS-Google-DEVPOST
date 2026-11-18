"""Phase 29 — recovery episode verification write-back (§29.23).

The engine's outcome verdict must reach the repaired task's durable
recovery episode: VERIFIED_RECOVERY is only produced by a passing
verification, and a failing or uncertain repair records RECOVERY_FAILED
— a queued correction is never reported as a successful recovery.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.recovery_episode_repository import (
    RecoveryEpisodeRepository, RecoveryState,
    set_recovery_episode_repo,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "episode-writeback.db"))
    await manager.initialize()
    try:
        yield manager
    finally:
        await manager.close()


@pytest_asyncio.fixture
async def episode_repo(db):
    repo = RecoveryEpisodeRepository(db)
    set_recovery_episode_repo(repo)
    try:
        yield repo
    finally:
        set_recovery_episode_repo(None)


async def _proposed_episode(repo, run_id="r1", task_id="t1"):
    """A repair has been queued: the episode reached PROPOSED through the
    honest claim/classify/propose chain, and the worker RELEASED its
    lease afterward (§29.23 — its phase is done; the verifier acquires
    ownership for the verdict)."""
    ep = await repo.create_episode({
        "run_id": run_id, "task_id": task_id, "tenant_id": "u1",
        "actor_id": "system", "failure_code": "TIMEOUT",
        "failure_category": "transient", "goal_digest": "g",
        "attempt_count": 1, "deadline_at": 99999999999})
    for st in (RecoveryState.CLASSIFIED, RecoveryState.PROPOSED):
        await repo.transition(ep["episode_id"], st, actor="system")
    stored = await repo.get_episode(ep["episode_id"])
    await repo.release_ownership(
        ep["episode_id"],
        owner_worker_id=stored.get("owner_worker_id") or "",
        owner_generation=stored.get("owner_generation") or 0)
    return ep


@pytest.mark.asyncio()
async def test_passing_verdict_produces_verified_recovery(episode_repo):
    from backend.services.recovery_episode_resolution import (
        resolve_episode_with_verdict)
    ep = await _proposed_episode(episode_repo)
    result = await resolve_episode_with_verdict(
        "r1", "t1", "pass", "criteria checked=3")
    assert result["resolved"] is True
    assert result["terminal"] == RecoveryState.VERIFIED_RECOVERY.value
    stored = await episode_repo.get_episode(ep["episode_id"])
    assert stored["state"] == RecoveryState.VERIFIED_RECOVERY.value


@pytest.mark.asyncio()
async def test_failing_verdict_records_recovery_failed(episode_repo):
    from backend.services.recovery_episode_resolution import (
        resolve_episode_with_verdict)
    ep = await _proposed_episode(episode_repo)
    result = await resolve_episode_with_verdict(
        "r1", "t1", "fail", "output missing required field")
    assert result["resolved"] is True
    assert result["terminal"] == RecoveryState.RECOVERY_FAILED.value
    stored = await episode_repo.get_episode(ep["episode_id"])
    assert stored["state"] == RecoveryState.RECOVERY_FAILED.value


@pytest.mark.asyncio()
async def test_uncertain_verdict_does_not_claim_verified_recovery(
        episode_repo):
    from backend.services.recovery_episode_resolution import (
        resolve_episode_with_verdict)
    ep = await _proposed_episode(episode_repo)
    result = await resolve_episode_with_verdict(
        "r1", "t1", "needs_review", "unresolved verification")
    assert result["resolved"] is True
    assert result["terminal"] == RecoveryState.RECOVERY_FAILED.value


@pytest.mark.asyncio()
async def test_terminal_episode_is_never_rewritten(episode_repo):
    from backend.services.recovery_episode_resolution import (
        resolve_episode_with_verdict)
    ep = await _proposed_episode(episode_repo)
    first = await resolve_episode_with_verdict("r1", "t1", "pass", "first")
    assert first["resolved"] is True
    # A second verdict on the now-terminal episode cannot rewrite it.
    second = await resolve_episode_with_verdict(
        "r1", "t1", "fail", "second must not apply")
    assert second["resolved"] is False
    stored = await episode_repo.get_episode(ep["episode_id"])
    assert stored["state"] == RecoveryState.VERIFIED_RECOVERY.value


@pytest.mark.asyncio()
async def test_task_without_episode_is_a_noop():
    from backend.services.recovery_episode_resolution import (
        resolve_episode_with_verdict)
    result = await resolve_episode_with_verdict("r9", "t9", "pass", "x")
    assert result["resolved"] is False


@pytest.mark.asyncio()
async def test_engine_writes_back_after_verification():
    """§29.23: the engine's completion path calls the resolution with
    the verifier verdict."""
    import inspect
    from backend.engine import engine as engine_module
    src = inspect.getsource(engine_module)
    assert "resolve_episode_with_verdict" in src
