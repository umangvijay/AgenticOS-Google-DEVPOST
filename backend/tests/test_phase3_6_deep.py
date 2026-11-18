"""Phase 3–6 deep verification (the second-pass acceptance rows).

Phase 3 §3.11/§4.10: renewal/release verify CURRENT ownership (strict
worker-id equality — an empty stored id never authorizes an arbitrary
caller).

Phase 4 §4.16: a webhook delivery's replay identity is never permanently
consumed by a temporary admission failure — the failed acceptance
RELEASES the replay key so the sender's retry can be accepted.
"""
import pytest
import pytest_asyncio

from backend.models.admission import ResourceClass
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.usage_repository import BucketLimit  # noqa



@pytest_asyncio.fixture
async def cap_repo(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager as DM
    db = DM(str(tmp_path / "p3deep.db"))
    await db.initialize()
    from backend.repositories.capacity_repository import SQLiteCapacityRepository
    repo = SQLiteCapacityRepository(db)
    try:
        yield repo
    finally:
        await db.close()


# ── Phase 3 §3.11/§4.10: ownership-verified renewal and release ────────

@pytest.mark.asyncio
async def test_wrong_worker_cannot_renew_empty_id_lease(cap_repo):
    """THE §3.11 bug fix: a lease with an empty stored worker_id REJECTS
    a renewal from an arbitrary caller — empty-id never authorizes
    anyone."""
    from backend.models.admission import ResourceClass
    outcome = await cap_repo.acquire(ResourceClass.WORKFLOW_TASK,
                                     "user:p3deep", "u1", "exec-a", limit=5)
    lease = outcome.lease
    assert lease.worker_id == ""   # acquired without a worker identity
    with pytest.raises(Exception):
        await cap_repo.renew(lease.lease_id, "worker-imposter",
                             lease.generation, 60)
    # The legitimate (empty-id) caller can still renew — extending beyond
    # the current expiry (renew is extend-only by design).
    renewed = await cap_repo.renew(lease.lease_id, "", lease.generation,
                                   3600)   # extend well past the default TTL
    assert renewed.expires_at_epoch > lease.expires_at_epoch


@pytest.mark.asyncio
async def test_wrong_worker_cannot_release_empty_id_lease(cap_repo):
    from backend.models.admission import ResourceClass
    outcome = await cap_repo.acquire(ResourceClass.WORKFLOW_TASK,
                                     "user:p3deep-rel", "u1", "exec-b",
                                     limit=5)
    with pytest.raises(Exception):
        await cap_repo.release(outcome.lease.lease_id, "worker-imposter",
                               outcome.lease.generation)
    # The lease is still active after the refused release.
    lease = await cap_repo.get_lease(outcome.lease.lease_id)
    assert lease.state.value == "active"


@pytest.mark.asyncio
async def test_stale_generation_cannot_renew(cap_repo):
    from backend.models.admission import ResourceClass
    outcome = await cap_repo.acquire(ResourceClass.WORKFLOW_TASK,
                                     "user:p3deep-gen", "u1", "exec-c",
                                     limit=5)
    with pytest.raises(Exception):
        await cap_repo.renew(outcome.lease.lease_id, "",
                             outcome.lease.generation + 5, 60)


# ── Phase 4 §4.16: replay-key release on failed acceptance ─────────────

@pytest.mark.asyncio
async def test_replay_key_release_allows_retry(tmp_path):
    """THE §4.16 acceptance: a delivery whose acceptance FAILED (queue
    full) does not permanently consume its replay identity — after the
    release, the same delivery is accepted."""
    from backend.repositories.sqlite.idempotency_repository import (
        SQLiteIdempotencyRepository)
    from backend.repositories.sqlite.database import DatabaseManager as DM
    db = DM(str(tmp_path / "p4deep.db"))
    await db.initialize()
    idem = SQLiteIdempotencyRepository(db)
    key = "webhook:probe-replay-1"
    # First delivery claims the key.
    assert await idem.atomic_insert(key, "wf-1", "webhook") is True
    # Acceptance FAILS → the key is released.
    released = await idem.release_replay_key(key)
    assert released is True
    # Double-release: nothing to release (no re-insert between them).
    assert await idem.release_replay_key(key) is False
    # The sender's RETRY is now accepted.
    assert await idem.atomic_insert(key, "wf-1", "webhook") is True
    await db.close()


@pytest.mark.asyncio
async def test_replay_key_release_never_touches_accepted_deliveries(tmp_path):
    """§4.16: an ACCEPTED delivery's key is never released by the failure
    path — only pending/running entries are releasable."""
    from backend.repositories.sqlite.idempotency_repository import (
        SQLiteIdempotencyRepository)
    from backend.repositories.sqlite.database import DatabaseManager as DM
    db = DM(str(tmp_path / "p4deep2.db"))
    await db.initialize()
    idem = SQLiteIdempotencyRepository(db)
    key = "webhook:accepted-1"
    await idem.atomic_insert(key, "wf-2", "webhook")
    await idem.commit_success(key, "run-1")   # the delivery was ACCEPTED
    released = await idem.release_replay_key(key)
    assert released is False, "accepted deliveries are never released"
    # The acceptance record survives.
    record = await idem.get_record(key)
    assert record is not None and record["status"] == "completed"
    await db.close()
