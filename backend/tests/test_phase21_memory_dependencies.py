"""Phase 21 §21.19: DURABLE memory dependencies. The dependency graph
lives in the repository (tenant-scoped rows), not process state —
correction and deletion propagate ACROSS RESTARTS, and failed cleanup
is retried visibly (the durable job row records attempts + errors)."""
import uuid

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def mem_stack(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.memory_repository import SQLiteMemoryRepository
    from backend.services.memory_lifecycle import MemoryLifecycleService
    from backend.services.memory_workflows import DurableDependencyMap
    db = DatabaseManager(str(tmp_path / "mem21.db"))
    await db.initialize()
    repo = SQLiteMemoryRepository(db)
    lifecycle = MemoryLifecycleService(repo)
    dep_map = DurableDependencyMap(repo)
    yield {"db": db, "repo": repo, "lifecycle": lifecycle,
           "deps": dep_map}
    await db.close()


async def test_dependency_survives_restart(mem_stack):
    """THE §21.19 acceptance: register a dependency, DESTROY the
    process-local map (a restart), and verify the edge still resolves
    from the DURABLE store."""
    repo, deps = mem_stack["repo"], mem_stack["deps"]
    uid = "u-mem21"
    source = await repo.store_memory(
        uid, "the staging endpoint accepts writes", "semantic",
        {"memory_class": "transient_context",
         "verification_status": "confirmed"}, embedding=[1.0, 0.0])
    # The summary derived FROM it registers as a dependent.
    summary = await repo.store_memory(
        uid, "summary: staging accepts writes", "semantic",
        {"memory_class": "execution_episode",
         "verification_status": "inferred"}, embedding=[0.9, 0.1])
    await deps.register_async(source, derived_kind="cached_summary",
                              derived_id=summary)
    # RESTART: a fresh DurableDependencyMap over the SAME repo (the
    # process map is gone).
    from backend.services.memory_workflows import DurableDependencyMap
    fresh = DurableDependencyMap(repo)
    edges = await fresh.dependents_async(source)
    normalized = [{k: e[k] for k in ("kind", "id") if k in e}
                  or {"kind": e.get("derived_kind"),
                      "id": e.get("derived_id")} for e in edges]
    flat = [{"kind": e.get("derived_kind", e.get("kind")),
             "id": e.get("derived_id", e.get("id"))} for e in edges]
    assert {"kind": "cached_summary", "id": summary} in flat, \
        f"the edge resolved from DURABLE storage after restart: {edges}"


async def test_correction_propagates_to_dependents_after_restart(mem_stack):
    repo, deps, lifecycle = (mem_stack["repo"], mem_stack["deps"],
                             mem_stack["lifecycle"])
    uid = "u-mem21b"
    source = await repo.store_memory(
        uid, "the API base is https://old.example.com", "semantic",
        {"memory_class": "factual", "verification_status": "confirmed"},
        embedding=[1.0])
    stale_summary = await repo.store_memory(
        uid, "summary: API base is old.example.com", "semantic",
        {"memory_class": "execution_episode",
         "verification_status": "inferred"}, embedding=[0.9, 0.1])
    await deps.register_async(source, derived_kind="cached_summary",
                              derived_id=stale_summary)
    # Restart + correction through the durable propagation path.
    from backend.services.memory_workflows import (
        DurableDependencyMap, propagate_correction)
    fresh = DurableDependencyMap(repo)
    result = await propagate_correction(
        memory_repo=repo, lifecycle=lifecycle, dependency_map=fresh,
        user_id=uid, memory_id=source,
        new_content="the API base is https://new.example.com",
        new_embedding=[0.8, 0.2])
    invalidated = result["invalidated_dependents"]
    flat = [{"kind": e.get("derived_kind", e.get("kind")),
             "id": e.get("derived_id", e.get("id"))} for e in invalidated]
    assert {"kind": "cached_summary", "id": stale_summary} in flat
    # The STALE summary is deleted from retrieval (cannot resurface).
    after = await repo.get_memory(uid, stale_summary)
    assert after is None or after.get("deletion_state") != "active", \
        "the stale summary no longer resurfaces after correction"


async def test_failed_cleanup_retried_visibly(mem_stack):
    """A failed cleanup is RETRIED and its failure is VISIBLE (the job
    row records attempts + the error) — never a silent fake success."""
    repo = mem_stack["repo"]
    uid = "u-mem21c"
    memory_id = await repo.store_memory(
        uid, "to be cleaned", "semantic",
        {"memory_class": "transient_context"}, embedding=[1.0])
    job_id = await repo.create_cleanup_job(
        memory_id=memory_id, user_id=uid,
        dependents=[{"kind": "cached_summary", "id": "summary-x"}])
    claimed = await repo.claim_cleanup_job("worker-a")
    assert claimed is not None and claimed["job_id"] == job_id
    # The worker fails mid-cleanup: progress is recorded with the error.
    await repo.record_cleanup_progress(job_id, worker_id="worker-a",
                                       state="failed",
                                       error="vault unlink failed")
    row = await repo.db.fetch_one(
        "SELECT * FROM memory_cleanup_jobs WHERE job_id=?", (job_id,))
    assert row["state"] == "failed" and "vault unlink" in row["last_error"], \
        "the failure is DURABLE and visible"
    # Retry: the job is claimable again (bounded retry) and succeeds.
    claimed2 = await repo.claim_cleanup_job("worker-b")
    assert claimed2 is not None
    await repo.record_cleanup_progress(job_id, worker_id="worker-b",
                                       state="complete")
    row2 = await repo.db.fetch_one(
        "SELECT * FROM memory_cleanup_jobs WHERE job_id=?", (job_id,))
    assert row2["state"] == "complete"
    assert row2["attempts"] >= 2, "the retry is counted"
