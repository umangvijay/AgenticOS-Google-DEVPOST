"""Phase 11 §11.21 acceptance tests: durable recovery episodes with atomic
claims, ownership races, CAS transitions, and abandoned-episode
reconciliation — through service/repo boundaries.
"""
import asyncio
import time

import pytest
import pytest_asyncio

from backend.models.admission import epoch_now
from backend.repositories.recovery_episode_repository import (
    RecoveryEpisodeRepository,
    RecoveryState,
    RecoveryTransitionError,
    recovery_transition_valid,
)


@pytest_asyncio.fixture
async def repo(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "recovery.db"))
    await db.initialize()
    yield RecoveryEpisodeRepository(db)
    await db.close()


def _episode(**overrides):
    defaults = dict(
        episode_id="ep_test", tenant_id="u1", actor_id="system",
        run_id="run-1", task_id="task-1", failure_code="TIMEOUT",
        failure_category="temporary_read_failure", goal_digest="gd-1",
        acceptance_criteria=[{"kind": "non_empty", "value": "reply"}],
    )
    defaults.update(overrides)
    return defaults


@pytest_asyncio.fixture
async def created(repo):
    return await repo.create_episode(_episode())


# ── §11.3: state machine ──────────────────────────────────────────────

def test_recovery_transition_matrix():
    assert recovery_transition_valid(RecoveryState.DETECTED,
                                     RecoveryState.CLASSIFIED, "system") is True
    assert recovery_transition_valid(RecoveryState.DETECTED,
                                     RecoveryState.PROPOSED, "agent") is False
    assert recovery_transition_valid(RecoveryState.VERIFYING,
                                     RecoveryState.VERIFIED_RECOVERY,
                                     "verifier") is True
    assert recovery_transition_valid(RecoveryState.VERIFIED_RECOVERY,
                                     RecoveryState.EXECUTING, "agent") is False


@pytest.mark.asyncio
async def test_episode_lifecycle_happy_path(repo, created):
    """§12.5: protected execution states require a CURRENT owner with a
    live lease — the lifecycle claims ownership before EXECUTING and
    carries the generation forward."""
    episode_id = created["episode_id"]
    for state, actor in [
        (RecoveryState.CLASSIFIED, "system"),
        (RecoveryState.PROPOSED, "agent"),
        (RecoveryState.VALIDATED, "system"),
        (RecoveryState.READY, "system"),
    ]:
        created = await repo.transition(episode_id, state, actor=actor)
    claimed = await repo.claim_episode(episode_id, "worker-A",
                                       lease_seconds=120)
    assert claimed is not None
    generation = claimed["owner_generation"]
    for state, actor in [
        (RecoveryState.EXECUTING, "agent"),
        (RecoveryState.VERIFYING, "system"),
        (RecoveryState.VERIFIED_RECOVERY, "verifier"),
    ]:
        created = await repo.transition(
            episode_id, state, actor=actor,
            require_live_lease=True,
            expected_owner_generation=generation)
        generation = created["owner_generation"]
    assert created["state"] == "VERIFIED_RECOVERY"


@pytest.mark.asyncio
async def test_stale_owner_cannot_commit_recovery_outcome(repo, created):
    """§12.5 acceptance: competing workers and redelivery cannot revive a
    stale owner — an expired lease blocks the protected transition."""
    import time as _time
    episode_id = created["episode_id"]
    for state, actor in [
        (RecoveryState.CLASSIFIED, "system"),
        (RecoveryState.PROPOSED, "agent"),
        (RecoveryState.VALIDATED, "system"),
        (RecoveryState.READY, "system"),
    ]:
        await repo.transition(episode_id, state, actor=actor)
    claimed = await repo.claim_episode(episode_id, "worker-A",
                                       lease_seconds=1)
    generation = claimed["owner_generation"]
    # Force the lease to expire.
    await repo.db.execute(
        "UPDATE recovery_episodes SET lease_expires_at=? WHERE episode_id=?",
        (_time.time() - 10, episode_id))
    await repo.db.commit()
    from backend.models.admission import epoch_now
    with pytest.raises(RecoveryTransitionError, match="stale"):
        await repo.transition(episode_id, RecoveryState.EXECUTING,
                              actor="agent", require_live_lease=True,
                              expected_owner_generation=generation)
    # A DIFFERENT worker reclaims (generation advances) and proceeds.
    reclaimed = await repo.claim_episode(episode_id, "worker-B",
                                         lease_seconds=120)
    assert reclaimed["owner_generation"] > generation


@pytest.mark.asyncio
async def test_invalid_transition_rejected(repo, created):
    with pytest.raises(RecoveryTransitionError):
        await repo.transition(created["episode_id"], RecoveryState.EXECUTING,
                              actor="agent")   # DETECTED → EXECUTING is invalid


# ── §11.7: atomic ownership claims ────────────────────────────────────

@pytest.mark.asyncio
async def test_two_workers_race_one_owner(repo, created):
    """§11.7/§11.19 acceptance: two workers race CONCURRENTLY — one owner."""
    episode_id = created["episode_id"]
    results = await asyncio.gather(
        repo.claim_episode(episode_id, "worker-1", 60),
        repo.claim_episode(episode_id, "worker-2", 60),
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1


@pytest.mark.asyncio
async def test_ownership_generation_advances(repo, created):
    episode_id = created["episode_id"]
    first = await repo.claim_episode(episode_id, "w-1", 30)
    gen_1 = first["owner_generation"]
    # Lease expires; another worker takes over → generation advances.
    await repo.db.execute(
        "UPDATE recovery_episodes SET lease_expires_at=0 WHERE episode_id=?",
        (episode_id,))
    await repo.db.commit()
    second = await repo.claim_episode(episode_id, "w-2", 30)
    assert second["owner_generation"] > gen_1


# ── §11.8: abandoned-episode reconciliation ───────────────────────────

@pytest.mark.asyncio
async def test_abandoned_episode_detected(repo, created):
    episode_id = created["episode_id"]
    # Simulate lease loss.
    await repo.db.execute(
        "UPDATE recovery_episodes SET lease_expires_at=0 WHERE episode_id=?",
        (episode_id,))
    await repo.db.commit()
    abandoned = await repo.abandoned_episodes()
    assert any(ep["episode_id"] == episode_id for ep in abandoned)


@pytest.mark.asyncio
async def test_crash_after_claim_episode_resumable(repo, created):
    """§11.19: crash after claim → episode becomes resumable."""
    episode_id = created["episode_id"]
    first = await repo.claim_episode(episode_id, "worker-crash", 30)
    assert first is not None
    # Simulate the worker crashing (lease expires, no state transition).
    await repo.db.execute(
        "UPDATE recovery_episodes SET lease_expires_at=0 WHERE episode_id=?",
        (episode_id,))
    await repo.db.commit()
    abandoned = await repo.abandoned_episodes()
    assert any(ep["episode_id"] == episode_id for ep in abandoned)
    # Another worker can claim and resume.
    resumed = await repo.claim_episode(episode_id, "worker-recovery", 60)
    assert resumed is not None
    assert resumed["owner_generation"] > first["owner_generation"]


# ── §11.9: persistent limits ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_attempt_counter_persists(repo, created):
    episode_id = created["episode_id"]
    await repo.increment_counters(episode_id, attempt_count=1, model_calls=2)
    ep = await repo.get_episode(episode_id)
    assert ep["attempt_count"] == 1 and ep["model_calls"] == 2
    await repo.increment_counters(episode_id, attempt_count=1, model_calls=1)
    ep = await repo.get_episode(episode_id)
    assert ep["attempt_count"] == 2 and ep["model_calls"] == 3


# ── §11.19: CAS race for transitions ──────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_transitions_one_wins(repo, created):
    """§11.3/§11.19: concurrent state transitions — one winner, one
    conflict."""
    episode_id = created["episode_id"]
    await repo.transition(episode_id, RecoveryState.CLASSIFIED, actor="system")

    results = await asyncio.gather(
        repo.transition(episode_id, RecoveryState.PROPOSED, actor="agent"),
        repo.transition(episode_id, RecoveryState.PROPOSED, actor="agent"),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, RecoveryTransitionError)]
    # Exactly one CAS transition wins; the other sees the already-changed
    # state and reports a conflict (§11.3/§11.19).
    assert len(successes) == 1, f"expected 1 success, got {successes}"
    assert len(conflicts) == 1, f"expected 1 conflict, got {conflicts}"
