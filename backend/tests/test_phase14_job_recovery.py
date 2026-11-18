"""Phase 14 §14.3: evaluation-job RECOVERY — the durable job survives
every crash window without losing evaluations or inflating counts.

- create-or-get identity: repeated terminal events return the SAME job.
- claim takeover: a dead worker's expired lease is recovered by another
  worker and the work completes exactly once.
- reconciliation: terminal runs discovered without a job get one
  (idempotently).
- exhausted jobs park VISIBLY and reset by operator action only.
"""
import pytest
import pytest_asyncio

from backend.repositories.evaluation_job_repository import (
    EvaluationJobRepository,
)
from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def jobs(tmp_path):
    db = DatabaseManager(str(tmp_path / "recovery.db"))
    await db.initialize()
    repo = EvaluationJobRepository(db)
    try:
        yield {"jobs": repo, "db": db}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_repeated_terminal_events_return_the_same_job(jobs):
    """The create-or-get identity — redelivery never inflates jobs."""
    jobs_repo = jobs["jobs"]
    first = await jobs_repo.enqueue("run-rec-1")
    second = await jobs_repo.enqueue("run-rec-1")
    third = await jobs_repo.enqueue("run-rec-1")
    assert first["job_id"] == second["job_id"] == third["job_id"]
    health = await jobs_repo.health()
    assert health["pending"] == 1


@pytest.mark.asyncio
async def test_dead_worker_lease_is_taken_over_and_completes_once(jobs):
    """The crash AFTER claim, BEFORE complete: the lease expires, another
    worker takes over, and the FIRST worker can no longer complete."""
    jobs_repo = jobs["jobs"]
    job = await jobs_repo.enqueue("run-rec-2")
    await jobs_repo.claim("worker-dead", lease_seconds=60)
    # The worker dies; time passes (lease expires).
    await jobs["db"].execute(
        "UPDATE evaluation_jobs SET lease_expires_at=1 WHERE job_id=?",
        (job["job_id"],))
    await jobs["db"].commit()
    # Another worker recovers it.
    taken = await jobs_repo.claim("worker-recovery")
    assert [j["job_id"] for j in taken] == [job["job_id"]]
    # The recovered worker completes; the dead one cannot.
    await jobs_repo.complete(job["job_id"], evaluation_id="eval-rec",
                             owner_worker_id="worker-recovery")
    from backend.repositories.evaluation_job_repository import (
        EvaluationJobError)
    with pytest.raises(EvaluationJobError, match="Stale worker"):
        await jobs_repo.complete(job["job_id"], evaluation_id="eval-rec",
                                 owner_worker_id="worker-dead")
    health = await jobs_repo.health()
    assert health["done"] == 1, "the evaluation happened exactly once"


@pytest.mark.asyncio
async def test_reconciliation_enqueues_missing_terminals_idempotently(jobs):
    """The crash BETWEEN terminal persistence and enqueue: the reconciler
    discovers terminal runs without jobs and enqueues them — running it
    twice does not duplicate."""
    jobs_repo = jobs["jobs"]
    n1 = await jobs_repo.enqueue_unfinished_terminals(
        ["run-orphan-1", "run-orphan-2"])
    assert n1 == 2
    n2 = await jobs_repo.enqueue_unfinished_terminals(
        ["run-orphan-1", "run-orphan-2", "run-orphan-3"])
    assert n2 == 1, "only the genuinely new run gets a job"
    health = await jobs_repo.health()
    assert health["pending"] == 3


@pytest.mark.asyncio
async def test_exhausted_jobs_park_visibly_and_reset_deliberately(jobs):
    """Bounded requeue → exhausted (visible in health) → operator reset.
    Failures are never silently dropped and never auto-retry forever."""
    jobs_repo = jobs["jobs"]
    job = await jobs_repo.enqueue("run-rec-4")
    for _ in range(5):
        await jobs_repo.claim("w")
        await jobs_repo.fail(job["job_id"], "boom", owner_worker_id="w")
    health = await jobs_repo.health()
    assert health["exhausted"] == 1 and health["pending"] == 0
    # The failure reason is durable.
    cursor = await jobs["db"].execute(
        "SELECT last_error, attempts FROM evaluation_jobs WHERE job_id=?",
        (job["job_id"],))
    row = await cursor.fetchone()
    assert row["last_error"] == "boom" and int(row["attempts"]) == 5
    # Operator reset requeues; evaluation can proceed.
    assert await jobs_repo.reset_exhausted() == 1
    claimed = await jobs_repo.claim("w2")
    assert claimed and claimed[0]["job_id"] == job["job_id"]


@pytest.mark.asyncio
async def test_drain_processes_only_what_it_owns(jobs):
    """The drainer completes/fails ONLY jobs it currently owns — a lease
    lost mid-flight is recovered by takeover, never double-processed."""
    jobs_repo = jobs["jobs"]
    await jobs_repo.enqueue("run-drain-1")
    processed = []
    async def run_evaluation(job):
        processed.append(job["run_id"])
        return f"eval-{job['run_id']}"
    n = await jobs_repo.drain("w-drain", batch=5,
                              run_evaluation=run_evaluation)
    assert n == 1 and processed == ["run-drain-1"]
    # A second drain finds nothing (the job is done — no reprocessing).
    n2 = await jobs_repo.drain("w-drain", batch=5,
                               run_evaluation=run_evaluation)
    assert n2 == 0 and len(processed) == 1
