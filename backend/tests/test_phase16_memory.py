"""Phase 16 acceptance tests: memory, retrieval, and knowledge lifecycle.

- §16.1 five classes; legacy types map through.
- §16.2 provenance + freshness: expired memories stop being served.
- §16.3 retrieval authorization: ownership before search and disclosure —
  similarity never grants cross-tenant access.
- §16.4 conflicts: a newer subject supersedes the older; history is
  preserved and inspectable, never silently merged.
- §16.5 user control: inspect, correct (new version), export, forget.
- §16.6 deletion propagation: supersedence pointers coherent after a
  forget; derived refs reported.
- §16.7 retrieval quality: search returns the KNOWN content and skips
  superseded/expired entries.
"""
import time

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.memory_repository import SQLiteMemoryRepository
from backend.services.memory_lifecycle import (
    MEMORY_CLASSES, MemoryLifecycleError, MemoryLifecycleService,
    normalize_memory_class,
)


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "memory-lifecycle.db"))
    await db.initialize()
    repo = SQLiteMemoryRepository(db)
    svc = MemoryLifecycleService(repo)
    try:
        yield {"repo": repo, "svc": svc, "db": db}
    finally:
        await db.close()


def _vec(seed: float) -> list:
    return [seed, seed + 0.1, seed + 0.2]


# ── §16.1 classification ───────────────────────────────────────────────

def test_memory_classes_normalized():
    for cls in MEMORY_CLASSES:
        assert normalize_memory_class(cls) == cls
    assert normalize_memory_class(None, "profile") == "preference"
    assert normalize_memory_class(None, "workflow") == "execution_episode"
    assert normalize_memory_class(None, "semantic") == "factual"
    assert normalize_memory_class("bogus") == "factual"


@pytest.mark.asyncio
async def test_unknown_class_refused(env):
    with pytest.raises(MemoryLifecycleError, match="Unknown memory class"):
        await env["svc"].remember(user_id="u1", content="x",
                                  memory_class="secret_class",
                                  embedding=_vec(1.0))


# ── §16.2 provenance + freshness ───────────────────────────────────────

@pytest.mark.asyncio
async def test_provenance_travels_with_the_memory(env):
    stored = await env["svc"].remember(
        user_id="u1", content="Prefers metric units",
        memory_class="preference", source="user_settings",
        confidence=0.95, embedding=_vec(1.0))
    assert stored["memory_class"] == "preference"
    assert stored["source"] == "user_settings"
    assert stored["confidence"] == 0.95


@pytest.mark.asyncio
async def test_expired_memory_is_not_served(env):
    """§16.2: expiry is honored at READ time — a memory whose expiry passes
    (simulated by aging the stored row) stops being served, never silently
    kept fresh."""
    svc = env["svc"]
    stale = await svc.remember(user_id="u1", content="stale fact",
                               memory_class="factual",
                               expires_at=int(time.time()) + 3600,
                               embedding=_vec(2.0))
    await svc.remember(user_id="u1", content="fresh fact",
                       memory_class="factual",
                       expires_at=int(time.time()) + 7200,
                       embedding=_vec(2.0))
    # Simulate time passing for the stale entry.
    await env["repo"].db.execute(
        "UPDATE memories SET expires_at=? WHERE id=?",
        (int(time.time()) - 10, stale["id"]))
    await env["repo"].db.commit()
    found = await svc.search("u1", _vec(2.0))
    contents = [r["content"] for r in found]
    assert "fresh fact" in contents
    assert "stale fact" not in contents, "expired memories stop being served"
    assert await svc.retrieve("u1", stale["id"]) is None


@pytest.mark.asyncio
async def test_cannot_store_already_expired(env):
    with pytest.raises(MemoryLifecycleError, match="expired"):
        await env["svc"].remember(user_id="u1", content="x", memory_class="factual",
                                  expires_at=int(time.time()) - 1,
                                  embedding=_vec(1.0))


# ── §16.3 authorized retrieval ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_similarity_never_grants_cross_tenant_access(env):
    """§16.3 acceptance: another tenant cannot read evaluation details or
    memories through search — even with an IDENTICAL embedding."""
    svc = env["svc"]
    await svc.remember(user_id="owner", content="secret project plan",
                       memory_class="factual", embedding=_vec(3.0))
    # A different tenant searches with the exact same vector.
    found = await svc.search("attacker", _vec(3.0))
    assert all(r["content"] != "secret project plan" for r in found)
    assert found == [], "no cross-tenant similarity leak"


@pytest.mark.asyncio
async def test_disclosure_requires_ownership(env):
    svc = env["svc"]
    stored = await svc.remember(user_id="owner", content="private note",
                                memory_class="factual", embedding=_vec(4.0))
    assert await svc.retrieve("owner", stored["id"]) is not None
    assert await svc.retrieve("attacker", stored["id"]) is None


# ── §16.4 conflicts ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_newer_entry_supersedes_older_history_preserved(env):
    svc = env["svc"]
    old = await svc.remember(user_id="u1", content="Lives in Berlin",
                             memory_class="factual",
                             subject_key="user:u1:city", embedding=_vec(5.0))
    new = await svc.remember(user_id="u1", content="Lives in Tokyo",
                             memory_class="factual",
                             subject_key="user:u1:city", embedding=_vec(5.0))
    found = await svc.search("u1", _vec(5.0))
    contents = [r["content"] for r in found]
    assert "Lives in Tokyo" in contents
    assert "Lives in Berlin" not in contents, "superseded entries are not served"
    # History preserved and inspectable.
    old_entry = await svc.retrieve("u1", old["id"])
    assert old_entry is not None
    assert old_entry["superseded_by"] == new["id"]


# ── §16.5 user control ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_correction_creates_a_new_version(env):
    svc = env["svc"]
    original = await svc.remember(user_id="u1", content="Works at Acme",
                                  memory_class="factual", source="resume_v1",
                                  embedding=_vec(6.0))
    corrected = await svc.correct("u1", original["id"],
                                  "Works at Acme as senior engineer",
                                  _vec(6.0))
    assert corrected["content"] == "Works at Acme as senior engineer"
    found = await svc.search("u1", _vec(6.0))
    assert [r["content"] for r in found] == \
        ["Works at Acme as senior engineer"]


@pytest.mark.asyncio
async def test_export_returns_own_memories_with_provenance(env):
    svc = env["svc"]
    await svc.remember(user_id="u1", content="a", memory_class="preference",
                       source="s1", embedding=_vec(7.0))
    await svc.remember(user_id="u2", content="b", memory_class="factual",
                       embedding=_vec(7.0))
    export = await svc.export_all("u1")
    assert export["count"] == 1
    assert export["memories"][0]["content"] == "a"
    assert export["memories"][0]["source"] == "s1"


# ── §16.6 deletion propagation ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_forget_deletes_and_keeps_history_coherent(env):
    svc = env["svc"]
    old = await svc.remember(user_id="u1", content="old fact",
                             memory_class="factual",
                             subject_key="subj:1", embedding=_vec(8.0))
    new = await svc.remember(user_id="u1", content="new fact",
                             memory_class="factual",
                             subject_key="subj:1", embedding=_vec(8.0))
    out = await svc.forget("u1", new["id"])
    assert out["deleted"] is True
    assert await svc.retrieve("u1", new["id"]) is None
    # §17.4 (finding B): deleting the CORRECTION never reactivates the
    # older fact it superseded — the dangling pointer keeps it dark.
    survivor = await svc.retrieve("u1", old["id"])
    assert survivor["superseded_by"] == new["id"], \
        "the pointer stays (dangling = still superseded)"
    found = await svc.search("u1", _vec(8.0))
    assert found == [], \
        "the old corrected fact cannot resurface through search"
    # A durable, claimable cleanup job replaced the in-process side effect.
    assert out.get("cleanup_job_id")
    job = await env["repo"].claim_cleanup_job("cleanup-worker")
    assert job and job["memory_id"] == new["id"]
    done = await env["repo"].record_cleanup_progress(
        job["job_id"], worker_id="cleanup-worker", state="complete")
    assert done is True


@pytest.mark.asyncio
async def test_forget_reports_derived_refs(env):
    svc = env["svc"]
    stored = await svc.remember(
        user_id="u1", content="indexed fact", memory_class="factual",
        embedding=_vec(9.0), metadata={"derived_refs": ["emb-cache:abc",
                                                        "dataset:case-7"]})
    out = await svc.forget("u1", stored["id"])
    assert out["deleted"] is True
    assert set(out["propagated"]) == {"emb-cache:abc", "dataset:case-7"}


# ── §16.7 retrieval quality ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_returns_known_content_precisely(env):
    svc = env["svc"]
    target = await svc.remember(user_id="u1", content="quarterly report location",
                                memory_class="procedure", embedding=_vec(10.0))
    await svc.remember(user_id="u1", content="unrelated cooking recipe",
                       memory_class="factual", embedding=_vec(50.0))
    found = await svc.search("u1", _vec(10.0))
    assert found[0]["content"] == "quarterly report location"
    assert found[0]["id"] == target["id"]
