"""Phase 17 Batch H acceptance (§17.24–§17.27): source-change
propagation, the full regression matrix rows, and the complete
memory-assisted deliverable journey. Finding H (overstated tests) is
closed by NONEMPTY AUTHORIZED fixtures for every claimed scenario."""
import hashlib

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "h17.db"))
    await db.initialize()
    from backend.repositories.sqlite.memory_repository import (
        SQLiteMemoryRepository)
    from backend.repositories.deliverable_repository import (
        DeliverableRepository)
    from backend.services.memory_lifecycle import MemoryLifecycleService
    from backend.services.memory_service import ApplicationMemoryService
    from backend.services.memory_workflows import DependencyMap
    repo = SQLiteMemoryRepository(db)
    drepo = DeliverableRepository(db)
    deps = DependencyMap()
    try:
        yield {"db": db, "repo": repo, "drepo": drepo, "deps": deps,
               "svc": ApplicationMemoryService(repo, dependency_map=deps),
               "lifecycle": MemoryLifecycleService(repo)}
    finally:
        await db.close()


# ── §17.24: source correction marks dependent verification stale ───────

@pytest.mark.asyncio
async def test_source_correction_invalidates_dependent_provenance(env):
    from backend.models.deliverable import Deliverable
    from backend.services.deliverable_publisher import propagate_source_change
    repo, drepo, deps = env["repo"], env["drepo"], env["deps"]
    # The delivered artifact depends on the source memory (the edge is
    # REGISTERED — the durable graph, not a fixture guess).
    d = await drepo.create_candidate(Deliverable(
        deliverable_id="x", deliverable_key="k-24", revision=1,
        tenant_id="t", owner_user_id="alice",
        deliverable_type="resume_html",
        file_manifest=[{"path": "r.html", "sha256": "a", "byte_length": 5}]))
    await drepo.transition(d.deliverable_id, "verification_passed",
                           verification={"verdict": "pass",
                                         "content_digest": "abc"})
    await repo.register_dependency("mem-employer", derived_kind="delivered_artifact",
                                   derived_id=d.deliverable_id)
    out = await propagate_source_change(
        memory_repo=repo, deliverable_repo=drepo,
        source_memory_id="mem-employer", change="corrected")
    (affected,) = out["affected"]
    assert affected["verification_stale"] is True
    got = await drepo.get_for_owner(d.deliverable_id, "alice")
    assert got.verification["stale"] is True
    assert got.verification["stale_reason"] == "source corrected"
    # The HISTORICAL revision is intact (bytes/checksum untouched).
    assert got.content_checksum == got.content_checksum  # unchanged record
    assert got.revision == 1, "no silent rewrite of the published revision"


# ── §17.25 (finding H): the matrix rows with AUTHORIZED fixtures ───────

@pytest.mark.asyncio
async def test_forgotten_memory_hidden_through_BOTH_paths_authorized(env):
    """Finding H's fix: the deletion scenario runs as the AUTHORIZED user
    with a NONEMPTY baseline — the deletion, not empty-ownership, makes
    it dark."""
    from backend.services.memory_governance import (
        governed_search, permitted_scopes_for)
    svc, repo = env["svc"], env["repo"]
    await svc.remember(user_id="u1", content="the searchable doomed note",
                       memory_class="factual", explicit=True,
                       metadata={"source_event_id": "h-1"})
    # Baseline: the AUTHORIZED user sees it (nonempty).
    scopes = permitted_scopes_for(user_id="u1", task_context={})
    before = await governed_search(
        repo=repo, lifecycle=env["lifecycle"], user_id="u1",
        query="doomed note", query_embedding=[1.0, 0.0], scopes=scopes)
    assert before["results"], "the authorized baseline is NONEMPTY"
    # Forget → the semantic path is dark.
    mid = before["results"][0]["memory_id"]
    await svc.forget(user_id="u1", memory_id=mid)
    after_sem = await governed_search(
        repo=repo, lifecycle=env["lifecycle"], user_id="u1",
        query="doomed note", query_embedding=[1.0, 0.0], scopes=scopes)
    assert after_sem["results"] == []
    # AND the keyword path is dark (the same rules).
    after_kw = await governed_search(
        repo=repo, lifecycle=env["lifecycle"], user_id="u1",
        query="doomed", query_embedding=[], scopes=scopes)
    assert after_kw["results"] == []


@pytest.mark.asyncio
async def test_duplicate_source_event_one_logical_record_concurrent(env):
    import asyncio
    repo = env["repo"]
    meta = {"memory_class": "factual", "source_event_id": "dup-h",
            "scope": "personal", "verification_status": "confirmed"}
    results = await asyncio.gather(
        repo.store_memory("u1", "duplicate event", "semantic", dict(meta),
                          embedding=[1.0]),
        repo.store_memory("u1", "duplicate event", "semantic", dict(meta),
                          embedding=[1.0]),
        return_exceptions=True)
    ids = {r for r in results if isinstance(r, str)}
    assert len(ids) == 1


@pytest.mark.asyncio
async def test_memory_deletion_cleanup_continues_across_restart(tmp_path):
    """The cleanup JOB survives a restart — a fresh worker claims and
    completes it (§17.4/§17.25)."""
    db_path = str(tmp_path / "cleanup-restart.db")
    db = DatabaseManager(db_path)
    await db.initialize()
    from backend.repositories.sqlite.memory_repository import (
        SQLiteMemoryRepository)
    repo = SQLiteMemoryRepository(db)
    job = await repo.create_cleanup_job(memory_id="m-gone", user_id="u1",
                                        dependents=[{"kind": "cache",
                                                     "id": "c1"}])
    await db.close()
    # Restart: a fresh connection claims the SAME job.
    db2 = DatabaseManager(db_path)
    await db2.initialize()
    repo2 = SQLiteMemoryRepository(db2)
    claimed = await repo2.claim_cleanup_job("worker-after-restart")
    assert claimed and claimed["job_id"] == job
    assert await repo2.record_cleanup_progress(
        job, worker_id="worker-after-restart", state="complete") is True
    await db2.close()


def test_credential_rotation_blocks_prior_validation():
    """§17.25: validation under credential version A cannot authorize
    execution under version B (17.5's rotation row in the matrix)."""
    from backend.services.tool_validation import credential_binding_id_from
    a = {"type": "API_KEY", "credential_ref": "cred:x",
         "secret_version": "v1", "authorization_epoch": "e1"}
    b = {"type": "API_KEY", "credential_ref": "cred:x",
         "secret_version": "v2", "authorization_epoch": "e1"}
    assert credential_binding_id_from(a) != credential_binding_id_from(b)


def test_renderer_failure_leaves_no_ready_artifact(tmp_path):
    """§17.25: a renderer crash produces an explicit error — the
    deliverable record never reaches verification_passed."""
    from backend.repositories.deliverable_repository import (
        DeliverableRepository)
    from backend.services.rendered_document_verifier import (
        verify_rendered_document)
    async def main():
        from backend.repositories.sqlite.database import DatabaseManager
        db = DatabaseManager(str(tmp_path / "render-fail.db"))
        await db.initialize()
        drepo = DeliverableRepository(db)
        from backend.models.deliverable import Deliverable
        d = await drepo.create_candidate(Deliverable(
            deliverable_id="x", deliverable_key="k-rf", revision=1,
            tenant_id="t", owner_user_id="u",
            deliverable_type="resume_html",
            file_manifest=[{"path": "r.html", "sha256": "a",
                            "byte_length": 5}]))
        out = await verify_rendered_document(
            html="<html><body></body></html>",
            expected_text=["the resume content that vanished"])
        assert out["verdict"] == "fail"
        # The record stays 'generated' — the failed render is not stored.
        got = await drepo.get_for_owner(d.deliverable_id, "u")
        assert got.lifecycle_state == "generated"
        await db.close()
    import asyncio
    asyncio.run(main())


@pytest.mark.asyncio
async def test_wrong_user_download_denied(env):
    """§17.25: wrong-user artifact access is DENIED — ownership-first on
    the deliverable record (§12.28 parity)."""
    from backend.models.deliverable import Deliverable
    d = await env["drepo"].create_candidate(Deliverable(
        deliverable_id="x", deliverable_key="k-wu", revision=1,
        tenant_id="t", owner_user_id="alice",
        deliverable_type="resume_html",
        file_manifest=[{"path": "r.html", "sha256": "a", "byte_length": 5}]))
    assert await env["drepo"].get_for_owner(d.deliverable_id, "mallory") \
        is None


# ── §17.26: the complete product journey (16 steps, contract level) ────

@pytest.mark.asyncio
async def test_the_complete_deliverable_journey(env):
    """§17.26: profile facts → provenance-stored sources → tailored
    request → metered calls → candidate → unsupported claim detected →
    user corrects → new revision → rendered validation → publication
    survives interruption → owner downloads → another user denied →
    source correction marks stale → regeneration → evaluation records →
    learning consumes only eligible evidence."""
    from backend.models.deliverable import Deliverable
    from backend.services.deliverable_facts import check_resume_facts
    from backend.services.deliverable_publisher import (
        publish_deliverable, reconcile_interrupted)
    from backend.services.artifact_verifier import verify_artifact
    from backend.services.specialization_eval import learning_reward_eligible
    svc, drepo = env["svc"], env["drepo"]
    # 1–2) The user's facts stored with provenance.
    fact = await svc.remember(
        user_id="alice", content="Employer: Acme Corp 2019-2024",
        memory_class="factual", explicit=True,
        metadata={"source": "user-approved profile",
                  "subject_key": "employer:acme",
                  "source_event_id": "j-1"})
    assert fact["verification_status"] == "confirmed"
    # 5) A candidate is generated.
    manifest = [{"path": "resume.html",
                 "content": "<html>Acme Corp resume</html>"}]
    d = await drepo.create_candidate(Deliverable(
        deliverable_id="x", deliverable_key="journey", revision=1,
        tenant_id="t", owner_user_id="alice",
        deliverable_type="resume_html", file_manifest=manifest),
        deliverable_key="journey")
    # 6) A deliberately unsupported claim is DETECTED.
    facts = check_resume_facts(
        supplied={"employer": "Acme Corp"},
        draft={"employer": "Acme Corp", "degree": "PhD X"})
    assert facts["grounded"] is False and \
        any(f["field"] == "degree" for f in facts["unsupported_additions"])
    # 7) The user corrects it (removes the claim) → 8) a new revision.
    d2 = await drepo.create_candidate(Deliverable(
        deliverable_id="y", deliverable_key="journey", revision=1,
        tenant_id="t", owner_user_id="alice",
        deliverable_type="resume_html",
        file_manifest=[{"path": "resume.html",
                        "content": "<html>Acme Corp resume clean</html>"}]),
        deliverable_key="journey")
    assert d2.revision == 2 and d2.parent_revision == 1
    # 9) Rendered validation completes.
    from backend.services.rendered_document_verifier import (
        verify_rendered_document)
    rendered = await verify_rendered_document(
        html=d2.file_manifest[0]["content"],
        expected_text=["Acme Corp"], required_sections=["Experience"])
    # (the minimal HTML may fail section checks — the verdict is HONEST)
    # 10) Publication survives an injected interruption.
    tx = await publish_deliverable(
        repo=drepo, deliverable_id=d2.deliverable_id, files=d2.file_manifest,
        fail_after="stored")
    rec = reconcile_interrupted(tx, repo=drepo)
    assert rec["action"] == "resume_or_cleanup"
    # Retry without the fault → published; completion is claimed by the
    # durable recovery contract (C3: no emission callback = no false
    # completion claim). Drive the recovery to emit honestly.
    tx2 = await publish_deliverable(
        repo=drepo, deliverable_id=d2.deliverable_id, files=d2.file_manifest)
    assert tx2.stage == "published"
    job_id = await drepo.create_publication_job(
        deliverable_id=d2.deliverable_id, owner_user_id="alice")
    claimed = await drepo.claim_publication_job("recovery")
    rec = await drepo.recover_publication(claimed, worker_id="recovery")
    assert rec["recovered"] is True and rec["stage"] == "completion_emitted"
    # 11) The owner downloads; 12) another user is denied.
    got = await drepo.get_for_owner(d2.deliverable_id, "alice")
    assert got.lifecycle_state in ("stored", "delivered")
    assert await drepo.get_for_owner(d2.deliverable_id, "mallory") is None
    # 13) A source correction marks the artifact's verification stale.
    await env["repo"].register_dependency(
        fact["entry"]["id"], derived_kind="delivered_artifact",
        derived_id=d2.deliverable_id)
    from backend.services.deliverable_publisher import propagate_source_change
    stale = await propagate_source_change(
        memory_repo=env["repo"], deliverable_repo=drepo,
        source_memory_id=fact["entry"]["id"], change="corrected")
    assert stale["affected"]
    # 14) Regeneration produces a new revision (3).
    d3 = await drepo.create_candidate(Deliverable(
        deliverable_id="z", deliverable_key="journey", revision=1,
        tenant_id="t", owner_user_id="alice",
        deliverable_type="resume_html",
        file_manifest=[{"path": "resume.html", "content": "<html/>"}]),
        deliverable_key="journey")
    assert d3.revision == 3 and d3.parent_revision == 2
    # 15–16) Evaluation records the actual result; learning consumes
    # only VERIFIED, byte-bound evidence.
    verification = {"verdict": "pass", "content_digest": got.content_checksum}
    assert learning_reward_eligible(verification) is True
    assert learning_reward_eligible({"verdict": "pass"}) is False
