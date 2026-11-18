"""Phase 14 finding G: evaluation-job fencing — the claim identity is
MANDATORY and every terminal transition is a single owner-verified CAS.

- An owner-less complete/fail is a contract error, not a bypass.
- A stale owner (lease expired or taken over) cannot complete OR fail.
- A non-owner cannot fail a live lease through the reconciler path.
- The reconciler may requeue only ownerless work.
"""
import pytest
import pytest_asyncio

from backend.repositories.evaluation_job_repository import (
    EvaluationJobError, EvaluationJobRepository,
)
from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def jobs(tmp_path):
    db = DatabaseManager(str(tmp_path / "fence.db"))
    await db.initialize()
    repo = EvaluationJobRepository(db)
    try:
        yield {"jobs": repo, "db": db}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ownerless_complete_is_a_contract_error(jobs):
    job = await jobs["jobs"].enqueue("run-g1")
    await jobs["jobs"].claim("w1")
    # The keyword is REQUIRED — Python's signature enforces it before
    # the repository can even run.
    with pytest.raises(TypeError):
        await jobs["jobs"].complete(job["job_id"], evaluation_id="e1")


@pytest.mark.asyncio
async def test_ownerless_fail_is_a_contract_error(jobs):
    job = await jobs["jobs"].enqueue("run-g2")
    await jobs["jobs"].claim("w1")
    with pytest.raises(TypeError):
        await jobs["jobs"].fail(job["job_id"], "boom")


@pytest.mark.asyncio
async def test_expired_lease_cannot_complete_even_by_former_owner(jobs):
    """The lease is the ownership window — past it, the former owner is
    stale and the takeover path recovers the job."""
    job = await jobs["jobs"].enqueue("run-g3")
    await jobs["jobs"].claim("w1", lease_seconds=60)
    await jobs["db"].execute(
        "UPDATE evaluation_jobs SET lease_expires_at=1 WHERE job_id=?",
        (job["job_id"],))
    await jobs["db"].commit()
    with pytest.raises(EvaluationJobError, match="Stale worker"):
        await jobs["jobs"].complete(job["job_id"], evaluation_id="e1",
                                    owner_worker_id="w1")
    # Takeover works, and the NEW owner completes.
    taken = await jobs["jobs"].claim("w2")
    assert taken and taken[0]["job_id"] == job["job_id"]
    await jobs["jobs"].complete(job["job_id"], evaluation_id="e2",
                                owner_worker_id="w2")


@pytest.mark.asyncio
async def test_non_owner_cannot_fail_a_live_lease(jobs):
    """A worker that never claimed the job cannot park it as failed —
    the reconciler path checks ownership before touching leased work."""
    job = await jobs["jobs"].enqueue("run-g4")
    await jobs["jobs"].claim("w1")
    await jobs["jobs"].fail_active_for_run("run-g4", "interfering",
                                           owner_worker_id="w2")
    state = (await jobs["jobs"].health())["by_state"]
    assert state.get("leased") == 1, "the live lease is untouched"
    # The OWNER can fail it, and the job requeues.
    await jobs["jobs"].fail_active_for_run("run-g4", "owner raised",
                                           owner_worker_id="w1")
    state = (await jobs["jobs"].health())["by_state"]
    assert state.get("pending") == 1


@pytest.mark.asyncio
async def test_completions_are_versioned_not_racy(jobs):
    """The completion CAS carries the state version — a takeover between
    the read and the write cannot slip an owner-less result through."""
    job = await jobs["jobs"].enqueue("run-g5")
    await jobs["jobs"].claim("w1")
    # The job completes exactly once; a second completion by the same
    # owner is refused (the state is no longer leased).
    await jobs["jobs"].complete(job["job_id"], evaluation_id="e1",
                                owner_worker_id="w1")
    with pytest.raises(EvaluationJobError, match="Stale worker"):
        await jobs["jobs"].complete(job["job_id"], evaluation_id="e1-dup",
                                    owner_worker_id="w1")
    row = await jobs["db"].execute(
        "SELECT evaluation_id FROM evaluation_jobs WHERE job_id=?",
        (job["job_id"],))
    assert (await row.fetchone())["evaluation_id"] == "e1"
