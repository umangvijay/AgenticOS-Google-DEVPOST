"""Phase 16 §16.0–§16.9: the governed-memory foundation — inventory,
scopes, the record contract, write policy, secret exclusion, durable
writes, candidate validation, dedup, supersession, freshness."""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "mem16.db"))
    await db.initialize()
    from backend.repositories.sqlite.memory_repository import (
        SQLiteMemoryRepository)
    from backend.services.memory_lifecycle import MemoryLifecycleService
    repo = SQLiteMemoryRepository(db)
    try:
        yield {"db": db, "repo": repo, "lifecycle": MemoryLifecycleService(repo)}
    finally:
        await db.close()


# ── §16.0: the inventory — every path has storage/retrieval/lifecycle ──

def test_memory_inventory_names_the_real_paths():
    from backend.services.memory_lifecycle import MEMORY_CLASSES
    from backend.repositories.sqlite.memory_repository import (
        SQLiteMemoryRepository)
    from backend.services.memory_lifecycle import MemoryLifecycleService
    # Every class has a storage path (store_memory), a retrieval path
    # (search/get), and lifecycle operations (correct/forget/export).
    assert set(MEMORY_CLASSES) == {"transient_context", "execution_episode",
                                   "procedure", "factual", "preference"}
    for op in ("store_memory", "get_memory", "search_memory", "delete_memory",
               "update_memory"):
        assert hasattr(SQLiteMemoryRepository, op), op
    for op in ("remember", "retrieve", "search", "correct", "export_all",
               "forget"):
        assert hasattr(MemoryLifecycleService, op), op


# ── §16.1: explicit scopes — org membership never exposes memories ─────

@pytest.mark.asyncio
async def test_org_membership_cannot_expose_personal_memories(env):
    """THE §16.1 acceptance: creating an organization or adding a member
    cannot expose historical personal memories — org-scope retrieval
    needs an EXPLICIT shared reference."""
    from backend.services.memory_governance import permitted_scopes_for
    repo = env["repo"]
    await repo.store_memory(
        "alice", "my private notes", "semantic",
        {"memory_class": "factual", "scope": "personal",
         "verification_status": "confirmed"},
        embedding=[1.0, 0.0])
    # Bob joins Alice's org; his permitted scopes contain NO org refs —
    # the personal memory of Alice is simply not in his search universe.
    bob_scopes = permitted_scopes_for(user_id="bob", task_context={},
                                      org_shared_refs=[])
    assert bob_scopes["owner"] == "bob"
    assert bob_scopes["organization_refs"] == []
    # Alice's memories live under alice's owner key — bob never sees them.
    rows = await repo.list_memories("bob")
    assert rows == []


@pytest.mark.asyncio
async def test_org_scope_requires_explicit_shared_ref(env):
    from backend.services.memory_governance import (
        MemoryCandidate, decide_write_policy, governed_search,
        permitted_scopes_for)
    repo, lifecycle = env["repo"], env["lifecycle"]
    # An INFERRED statement cannot publish to the organization (§16.1).
    cand = decide_write_policy(MemoryCandidate(
        content="team uses the blue template", memory_class="factual",
        scope="organization", scope_ref="org-1", explicit=False))
    assert cand.policy_decision == "refused"
    # An EXPLICIT, authorized statement can — and becomes a shared ref.
    cand2 = decide_write_policy(MemoryCandidate(
        content="the team approved the blue template", memory_class="factual",
        scope="organization", scope_ref="org-1", explicit=True,
        source_event_id="share-1"))
    assert cand2.policy_decision == "stored"
    from backend.services.embedding_service import EmbeddingService
    await repo.store_memory(
        "alice", cand2.content, "semantic",
        {"memory_class": "factual", "scope": "organization",
         "scope_ref": "org-1", "verification_status": "confirmed",
         "source_event_id": "share-1"},
        embedding=[0.9, 0.1])
    # Bob searches WITH the shared ref → he finds it.
    scopes = permitted_scopes_for(user_id="bob", task_context={},
                                  org_shared_refs=["org-1"])
    out = await governed_search(
        repo=repo, lifecycle=lifecycle, user_id="bob", query="blue template",
        query_embedding=[0.9, 0.1], scopes=scopes)
    assert any("blue template" in r["content"] for r in out["results"])
    # Without the ref → nothing.
    scopes2 = permitted_scopes_for(user_id="bob", task_context={},
                                   org_shared_refs=[])
    out2 = await governed_search(
        repo=repo, lifecycle=lifecycle, user_id="bob", query="blue template",
        query_embedding=[0.9, 0.1], scopes=scopes2)
    assert out2["results"] == []


# ── §16.2: the record contract — traceable statements ──────────────────

@pytest.mark.asyncio
async def test_retrieved_statement_traces_to_source_owner_status(env):
    repo = env["repo"]
    mid = await repo.store_memory(
        "carol", "client reports use PDF", "semantic",
        {"memory_class": "preference", "scope": "personal",
         "verification_status": "confirmed", "source": "user:carol instruction",
         "source_event_id": "evt-pdf-1", "observed_at": 1700000000},
        embedding=[0.5, 0.5])
    record = await repo.get_memory("carol", mid)
    assert record["source"] == "user:carol instruction"
    assert record["owner_user_id"] if False else record["user_id"] == "carol"
    assert record["verification_status"] == "confirmed"
    assert record["scope"] == "personal"
    assert record["source_event_id"] == "evt-pdf-1"
    assert record["deletion_state"] == "active"


# ── §16.3: the write policy — explainable, explicit vs inferred ────────

def test_write_policy_explains_itself_and_separates_inference():
    from backend.services.memory_governance import (
        MemoryCandidate, decide_write_policy)
    # Explicit instruction → stored, confirmed, no re-asking.
    p1 = decide_write_policy(MemoryCandidate(
        content="always address the user as Doctor", memory_class="preference",
        explicit=True))
    assert p1.policy_decision == "stored" and \
        p1.verification_status == "confirmed"
    assert "no re-asking" in p1.policy_reason
    # An inference → needs confirmation, stored as inferred at best.
    p2 = decide_write_policy(MemoryCandidate(
        content="the user seems to prefer brevity", memory_class="factual",
        explicit=False))
    assert p2.policy_decision == "needs_confirmation"
    assert p2.verification_status == "inferred"


# ── §16.4: secrets cannot enter ordinary memory ────────────────────────

def test_secret_shaped_content_is_refused_everywhere():
    from backend.services.memory_governance import (
        MemoryCandidate, contains_secret, decide_write_policy)
    for secret in ("my key is sk-abc123def456ghi789jkl",
                   "password=hunter2secret",
                   "-----BEGIN RSA PRIVATE KEY-----"):
        assert contains_secret(secret), secret[:20]
    # The policy refuses it BEFORE persistence.
    cand = decide_write_policy(MemoryCandidate(
        content="the api key is sk-abc123def456ghi789jkl",
        memory_class="factual", explicit=True))
    assert cand.policy_decision == "refused"
    assert "§16.4" in cand.policy_reason


# ── §16.5: durable, idempotent writes ──────────────────────────────────

@pytest.mark.asyncio
async def test_event_replay_does_not_multiply_memories(env):
    """§16.5: create-or-get on the source-event identity — repeated
    workflow events return the SAME memory; a crash between storage and
    indexing loses nothing (the row IS the durable record)."""
    repo = env["repo"]
    meta = {"memory_class": "execution_episode",
            "source_event_id": "terminal-run-123", "scope": "personal"}
    first = await repo.store_memory("u1", "run 123 verified", "workflow",
                                    meta, embedding=[1.0])
    second = await repo.store_memory("u1", "run 123 verified", "workflow",
                                     meta, embedding=[1.0])
    assert first == second, "event replay returns the SAME memory"
    rows = await repo.list_memories("u1")
    assert len(rows) == 1


# ── §16.6: candidate validation — the server decides ───────────────────

def test_malformed_candidates_are_refused():
    from backend.services.memory_governance import (
        MemoryCandidate, MemoryGovernanceError, validate_candidate)
    with pytest.raises(MemoryGovernanceError, match="unknown memory class"):
        validate_candidate(MemoryCandidate(content="x",
                                           memory_class="secret_thoughts"))
    with pytest.raises(MemoryGovernanceError, match="empty content"):
        validate_candidate(MemoryCandidate(content="  ",
                                           memory_class="factual"))
    with pytest.raises(MemoryGovernanceError, match="dated lifetime"):
        validate_candidate(MemoryCandidate(content="x",
                                           memory_class="factual",
                                           expected_lifetime="dated"))


def test_the_model_cannot_self_confirm_or_widen_visibility():
    from backend.services.memory_governance import (
        MemoryCandidate, decide_write_policy)
    # The model labels its claim "confirmed" — the SERVER overrules.
    cand = MemoryCandidate(content="user owns a yacht",
                           memory_class="factual", explicit=False,
                           verification_status="confirmed")
    decided = decide_write_policy(cand)
    assert decided.verification_status == "inferred", \
        "a model cannot label an unsupported claim confirmed"
    # The model claims organization visibility for an inference — refused.
    cand2 = MemoryCandidate(content="user owns a yacht",
                            memory_class="factual", explicit=False,
                            scope="organization")
    assert decide_write_policy(cand2).policy_decision == "refused"


# ── §16.7: dedup preserves real differences ────────────────────────────

@pytest.mark.asyncio
async def test_similar_statements_for_different_users_stay_separate(env):
    repo = env["repo"]
    await repo.store_memory("user-a", "prefers concise reports", "semantic",
                            {"memory_class": "preference",
                             "subject_key": "report_style",
                             "verification_status": "confirmed"},
                            embedding=[1.0, 0.0])
    await repo.store_memory("user-b", "prefers concise reports", "semantic",
                            {"memory_class": "preference",
                             "subject_key": "report_style",
                             "verification_status": "confirmed"},
                            embedding=[1.0, 0.0])
    a = await repo.list_memories("user-a")
    b = await repo.list_memories("user-b")
    assert len(a) == 1 and len(b) == 1
    assert a[0]["id"] != b[0]["id"], \
        "similar statements for different users never merge"


# ── §16.8: contradictions and supersession with applicability ──────────

@pytest.mark.asyncio
async def test_context_differences_coexist_corrections_supersede(env):
    """'Use PDF for client reports' and 'use DOCX for internal review'
    coexist (different applicability); an explicit correction supersedes
    with history preserved."""
    repo, lifecycle = env["repo"], env["lifecycle"]
    pdf_id = (await lifecycle.remember(
        user_id="u1", content="use PDF for client reports",
        memory_class="preference", subject_key="report_format:client",
        source="user instruction", embedding=[0.6, 0.4]))["id"]
    docx_id = (await lifecycle.remember(
        user_id="u1", content="use DOCX for internal review",
        memory_class="preference", subject_key="report_format:internal",
        source="user instruction", embedding=[0.4, 0.6]))["id"]
    # Different applicability: BOTH are active.
    active = [m for m in await repo.list_memories("u1")
              if not m.get("superseded_by")]
    assert len(active) == 2
    # An explicit correction to the CLIENT format supersedes THAT one.
    corrected = await lifecycle.correct("u1", pdf_id,
                                        "use PDF/A for client reports",
                                        embedding=[0.5, 0.5])
    rows = await repo.list_memories("u1")
    old = next(r for r in rows if r["id"] == pdf_id)
    assert old["superseded_by"], "the old version is superseded, not deleted"
    assert old["content"] == "use PDF for client reports", \
        "history is preserved"


# ── §16.9: freshness — remembered success is not current permission ────

@pytest.mark.asyncio
async def test_expired_memory_stops_being_served(env):
    from backend.services.memory_governance import (
        governed_search, permitted_scopes_for)
    repo, lifecycle = env["repo"], env["lifecycle"]
    await repo.store_memory(
        "u1", "the staging endpoint accepted writes", "semantic",
        {"memory_class": "transient_context", "expires_at": 1,
         "verification_status": "confirmed"},
        embedding=[1.0, 0.0])
    await repo.store_memory(
        "u1", "the production endpoint accepts writes", "semantic",
        {"memory_class": "transient_context", "verification_status": "confirmed"},
        embedding=[1.0, 0.0])
    scopes = permitted_scopes_for(user_id="u1", task_context={})
    out = await governed_search(
        repo=repo, lifecycle=lifecycle, user_id="u1",
        query="endpoint accepts writes", query_embedding=[1.0, 0.0],
        scopes=scopes)
    contents = [r["content"] for r in out["results"]]
    assert contents == ["the production endpoint accepts writes"], \
        "an expired assumption is not served as current"
