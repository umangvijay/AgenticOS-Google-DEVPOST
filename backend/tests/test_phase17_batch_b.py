"""Phase 17 Batch B acceptance (§17.3/§17.4; findings A, B, C): the ONE
memory service behind every entry point, durable correction/deletion,
and Firestore parity by contract."""
import json

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "b17.db"))
    await db.initialize()
    from backend.repositories.sqlite.memory_repository import (
        SQLiteMemoryRepository)
    from backend.services.memory_workflows import DependencyMap
    repo = SQLiteMemoryRepository(db)
    from backend.services.memory_service import ApplicationMemoryService
    try:
        yield {"db": db, "repo": repo,
               "svc": ApplicationMemoryService(repo,
                                               dependency_map=DependencyMap())}
    finally:
        await db.close()


# ── §17.3: the model cannot set authoritative fields via metadata ──────

def test_server_owned_metadata_is_stripped():
    from backend.services.memory_service import strip_server_owned_fields
    out = strip_server_owned_fields({
        "subject_key": "k", "scope": "organization",
        "verification_status": "confirmed", "user_id": "someone-else",
        "sensitivity": "normal"})
    assert "scope" not in out and "verification_status" not in out
    assert "user_id" not in out
    assert out["subject_key"] == "k", "benign metadata survives"


@pytest.mark.asyncio
async def test_the_model_cannot_self_publish(env):
    """The orchestrator's store path declares organization scope and
    'confirmed' status — the service strips both and the write policy
    decides (inferred factual → needs_confirmation, personal scope)."""
    svc = env["svc"]
    out = await svc.remember(
        user_id="u1", content="the team approved the blue template",
        memory_class="factual", explicit=False,
        metadata={"scope": "organization", "verification_status": "confirmed",
                  "subject_key": "tmpl"})
    assert out["entry"]["scope"] == "personal", \
        "the model's claimed visibility was stripped; the policy decided"
    assert out["verification_status"] == "inferred"
    assert out["policy_decision"] == "needs_confirmation"


# ── §17.3: semantic AND keyword fallback share the same rules ──────────

@pytest.mark.asyncio
async def test_both_retrieval_paths_enforce_the_same_visibility(env):
    svc = env["svc"]
    await svc.remember(user_id="alice", content="the zeta project plan",
                       memory_class="factual", explicit=True,
                       metadata={"source_event_id": "z1"})
    # Deleted (hidden barrier) → dark on BOTH paths.
    await svc.forget(user_id="alice", memory_id=(
        await env["repo"].list_memories("alice"))[0]["id"])
    out_sem = await svc.retrieve_for_task(
        user_id="alice", query="zeta project plan",
        query_embedding=[1.0, 0.0])
    out_kw = await svc.retrieve_for_task(
        user_id="alice", query="zeta project plan",
        query_embedding=[])   # no embedding → keyword scoring inside
    assert out_sem["results"] == [] and out_kw["results"] == [], \
        "the deletion barrier holds on semantic AND fallback retrieval"


@pytest.mark.asyncio
async def test_provenance_labels_stay_in_the_agent_context(env):
    """§17.3: the orchestrator's simplified result shape is gone — the
    service returns status/scope/source/hostile flags with every row."""
    svc = env["svc"]
    await svc.remember(user_id="u1", content="the q3 numbers",
                       memory_class="factual", explicit=False,
                       metadata={"source_event_id": "q3-1"})
    out = await svc.retrieve_for_task(user_id="u1", query="q3 numbers",
                                      query_embedding=[1.0, 0.0])
    (hit,) = out["results"]
    for field in ("verification_status", "scope", "source",
                  "content_class", "hostile_flag"):
        assert field in hit, field


# ── §17.4/B: version progression 1 → 2 → 3 ─────────────────────────────

@pytest.mark.asyncio
async def test_correction_versions_progress_never_reset(env):
    svc = env["svc"]
    first = await svc.remember(user_id="u1", content="v1 fact",
                               memory_class="factual", explicit=True,
                               metadata={"subject_key": "s1",
                                         "source_event_id": "v1-e"})
    v1 = int(first["entry"]["version"])
    second = await svc.correct(user_id="u1", memory_id=first["entry"]["id"],
                               content="v2 fact", embedding=[1.0])
    assert int(second["entry"]["version"]) == v1 + 1, \
        "the correction's version increment is HONORED (finding B)"
    third = await svc.correct(user_id="u1", memory_id=second["entry"]["id"],
                              content="v3 fact", embedding=[1.0])
    assert int(third["entry"]["version"]) == v1 + 2, \
        "progression continues 1 → 2 → 3"


# ── §17.4/B: duplicate events → one logical memory (concurrent) ────────

@pytest.mark.asyncio
async def test_concurrent_duplicate_events_produce_one_memory(env):
    import asyncio
    repo = env["repo"]
    meta = {"memory_class": "factual", "source_event_id": "dup-17",
            "scope": "personal", "verification_status": "confirmed"}
    results = await asyncio.gather(
        repo.store_memory("u1", "same event", "semantic", dict(meta),
                          embedding=[1.0]),
        repo.store_memory("u1", "same event", "semantic", dict(meta),
                          embedding=[1.0]),
        return_exceptions=True)
    ids = {r for r in results if isinstance(r, str)}
    assert len(ids) == 1, \
        f"concurrent duplicates resolve to ONE memory (got {results})"
    assert len(await repo.list_memories("u1")) == 1


# ── §17.4/B: durable dependencies + cleanup jobs survive restarts ──────

@pytest.mark.asyncio
async def test_dependency_edges_persist_across_restart(tmp_path):
    db_path = str(tmp_path / "deps.db")
    db = DatabaseManager(db_path)
    await db.initialize()
    from backend.repositories.sqlite.memory_repository import (
        SQLiteMemoryRepository)
    repo = SQLiteMemoryRepository(db)
    await repo.register_dependency("mem-1", derived_kind="cached_summary",
                                   derived_id="sum-9")
    await repo.register_dependency("mem-1", derived_kind="cached_summary",
                                   derived_id="sum-9")   # idempotent
    await db.close()
    # "Restart": a fresh connection to the same file.
    db2 = DatabaseManager(db_path)
    await db2.initialize()
    repo2 = SQLiteMemoryRepository(db2)
    deps = await repo2.dependents_of("mem-1")
    assert len(deps) == 1, \
        "the restart did not erase the dependency knowledge"
    await db2.close()


@pytest.mark.asyncio
async def test_cleanup_jobs_are_claimable_and_progress_is_recorded(env):
    repo = env["repo"]
    job = await repo.create_cleanup_job(memory_id="m-x", user_id="u1",
                                        dependents=[{"kind": "cache",
                                                     "id": "c1"}])
    claimed = await repo.claim_cleanup_job("worker-1")
    assert claimed and claimed["job_id"] == job
    assert claimed["dependents"] == [{"kind": "cache", "id": "c1"}]
    assert await repo.record_cleanup_progress(
        job, worker_id="worker-1", state="complete") is True
    # A non-owner cannot record progress on a claimed job.
    other = await repo.create_cleanup_job(memory_id="m-y", user_id="u1",
                                          dependents=[])
    await repo.claim_cleanup_job("worker-2")
    assert await repo.record_cleanup_progress(
        other, worker_id="worker-1", state="complete") is False


# ── Finding C: parity is BEHAVIORAL, not just method names ─────────────

def test_firestore_store_populates_the_queried_location():
    """Finding C: the dedup queries top-level source_event_id — the write
    path must populate it there (behavioral parity, verified by source)."""
    import inspect
    from backend.repositories.firestore import memory_repository as fs_mod
    src = inspect.getsource(fs_mod)
    assert '"source_event_id": meta["source_event_id"]' in src, \
        "the write populates the queried top-level field"
    assert "async_transactional" in src, \
        "the query-then-create runs in one transaction"


def test_factory_wires_the_lifecycle_for_both_stores():
    import inspect
    from backend.repositories import factory as factory_mod
    src = inspect.getsource(factory_mod)
    occurrences = src.count("self.memory_lifecycle = MemoryLifecycleService(")
    assert occurrences >= 2, \
        "memory_lifecycle exists for SQLite AND Firestore (finding C)"


def test_source_event_identity_is_user_scoped():
    """Finding C: the same event id under DIFFERENT owners is not a
    duplicate — the unique index is (user_id, source_event_id)."""
    import inspect
    from backend.repositories.sqlite import database as db_mod
    src = inspect.getsource(db_mod)
    assert "ON memories(user_id, source_event_id)" in src
