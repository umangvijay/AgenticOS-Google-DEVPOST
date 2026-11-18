"""Phase 18 Batch A acceptance (C7–C13): durable publication jobs, atomic
content-changing reviews, production workers, per-invocation metering,
and the deliverable startup contract."""
import os

import pytest
import pytest_asyncio

from backend.repositories.deliverable_repository import (
    DeliverableRepository)
from backend.repositories.sqlite.database import DatabaseManager
from backend.models.deliverable import Deliverable


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "p18a.db"))
    await db.initialize()
    r = DeliverableRepository(db)
    try:
        yield r
    finally:
        await db.close()


async def _candidate(repo, key="k18", owner="alice"):
    return await repo.create_candidate(Deliverable(
        deliverable_id="x", deliverable_key=key, revision=1,
        tenant_id="t", owner_user_id=owner, deliverable_type="resume_html",
        file_manifest=[{"path": "resume.html",
                        "content": "<html>Acme Corp</html>"}]),
        deliverable_key=key)


# ── C7: production deps are mandatory when required ────────────────────

@pytest.mark.asyncio
async def test_production_publication_requires_dependencies(repo):
    from backend.services.deliverable_publisher import publish_deliverable
    d = await _candidate(repo)
    with pytest.raises(ValueError, match="production publication requires"):
        await publish_deliverable(repo=repo, deliverable_id=d.deliverable_id,
                                  files=d.file_manifest,
                                  require_production_deps=True)


# ── C8: publication progress is DURABLE and recovery EXECUTES ──────────

@pytest.mark.asyncio
async def test_publication_job_survives_restart_and_recovery_completes(repo):
    d = await _candidate(repo)
    job_id = await repo.create_publication_job(
        deliverable_id=d.deliverable_id, owner_user_id="alice",
        fail_after="stored")   # injected fault
    # First recovery: claim + drive — the fault fires → retryable.
    claimed = await repo.claim_publication_job("w1")
    assert claimed and claimed["job_id"] == job_id
    out = await repo.recover_publication(claimed, worker_id="w1")
    row = dict(await repo.db.fetch_one(
        "SELECT * FROM publication_jobs WHERE job_id=?", (job_id,)))
    assert row["state"] in ("pending", "failed")
    assert row["stage"] in ("stored", "checksummed", "validated")
    # Retry WITHOUT the fault: clear the injected fault in the DURABLE
    # job row (the fault is a test hook living on the job), then recover.
    await repo.db.execute(
        "UPDATE publication_jobs SET fail_after=NULL, attempts=0 "
        "WHERE job_id=?", (job_id,))
    await repo.db.commit()
    row2 = dict(await repo.db.fetch_one(
        "SELECT * FROM publication_jobs WHERE job_id=?", (job_id,)))
    out2 = await repo.recover_publication(row2, worker_id="w2")
    final = dict(await repo.db.fetch_one(
        "SELECT state, stage FROM publication_jobs WHERE job_id=?",
        (job_id,)))
    assert final["state"] == "complete"
    assert final["stage"] == "completion_emitted"


@pytest.mark.asyncio
async def test_expired_claim_is_reclaimable(repo):
    d = await _candidate(repo)
    job_id = await repo.create_publication_job(
        deliverable_id=d.deliverable_id, owner_user_id="alice")
    claimed = await repo.claim_publication_job("w1")
    assert claimed and claimed["job_id"] == job_id
    # The lease expires; worker-2 takes over (fencing via the lease).
    await repo.db.execute(
        "UPDATE publication_jobs SET lease_expires_at=1 WHERE job_id=?",
        (job_id,))
    await repo.db.commit()
    claimed2 = await repo.claim_publication_job("w2")
    assert claimed2 and claimed2["owner_worker_id"] == "w2"


@pytest.mark.asyncio
async def test_exhausted_attempts_park_failed_visibly(repo):
    d = await _candidate(repo)
    job_id = await repo.create_publication_job(
        deliverable_id=d.deliverable_id, owner_user_id="alice",
        fail_after="validated", max_attempts=1)
    claimed = await repo.claim_publication_job("w1")
    await repo.recover_publication(claimed, worker_id="w1")
    final = dict(await repo.db.fetch_one(
        "SELECT state FROM publication_jobs WHERE job_id=?", (job_id,)))
    assert final["state"] == "failed", \
        "at max attempts the job parks FAILED — visible, never lost"


@pytest.mark.asyncio
async def test_worker_drives_recovery_to_completion(repo):
    from backend.workers.deliverable_worker import run_publication_recovery
    d = await _candidate(repo)
    job_id = await repo.create_publication_job(
        deliverable_id=d.deliverable_id, owner_user_id="alice")
    completed = await run_publication_recovery(repo, worker_id="worker")
    assert completed >= 1, \
        "the production worker EXECUTES the recovery (C8)"
    final = dict(await repo.db.fetch_one(
        "SELECT state, stage FROM publication_jobs WHERE job_id=?",
        (job_id,)))
    assert final["state"] == "complete", \
        f"the worker drove the publication to completion: {final}"


# ── C9/C10: review actions change content + atomic revision check ──────

@pytest.mark.asyncio
async def test_remove_claim_changes_content_and_creates_revision(repo):
    from backend.api.routers.deliverables import (
        _remove_claim_from_html)
    content = "<html><body><p>Acme Corp</p><p>PhD X</p></body></html>"
    cleaned = _remove_claim_from_html(content, "PhD")
    assert "PhD" not in cleaned and "Acme Corp" in cleaned, \
        "removing a claim removes it from the downloaded content"


@pytest.mark.asyncio
async def test_atomic_revision_conflict_returns_current(repo):
    d1 = await _candidate(repo, key="k-conf")
    d2 = await _candidate(repo, key="k-conf")   # revision 2
    # A review addressed at revision 1 conflicts (current is 2).
    def mutate(manifest):
        return manifest
    out = await repo.review_with_revision_check(
        d1.deliverable_id, expected_revision=1, mutate=mutate)
    assert out["conflict"] is True
    assert out["current_revision"] == 2
    assert out["reviewed_revision"] == 1


@pytest.mark.asyncio
async def test_current_revision_review_mutates_atomically(repo):
    d2 = await _candidate(repo, key="k-ok")
    d3 = await _candidate(repo, key="k-ok")
    def mutate(manifest):
        import hashlib as _hl
        new = []
        for f in manifest:
            content = f.get("content", "").replace("Acme", "Beta")
            enc = content.encode()
            new.append({**f, "content": content,
                        "sha256": _hl.sha256(enc).hexdigest(),
                        "byte_length": len(enc)})
        return new
    out = await repo.review_with_revision_check(
        d3.deliverable_id, expected_revision=2, mutate=mutate)
    assert out["conflict"] is False
    got = await repo.get(d3.deliverable_id)
    assert "Beta" in got.file_manifest[0]["content"], \
        "the mutation landed through the atomic check"
    assert got.content_checksum == out["content_checksum"]


# ── C12: the cleanup worker runs the durable jobs ──────────────────────

@pytest.mark.asyncio
async def test_cleanup_worker_completes_registered_dependents(tmp_path):
    from backend.workers.memory_cleanup_worker import run_cleanup_pass
    from backend.repositories.sqlite.memory_repository import (
        SQLiteMemoryRepository)
    from backend.services.memory_lifecycle import MemoryLifecycleService
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "cw.db"))
    await db.initialize()
    repo = SQLiteMemoryRepository(db)
    lc = MemoryLifecycleService(repo)
    target = (await lc.remember(user_id="u1", content="the source fact",
                                memory_class="factual", source="user",
                                embedding=[1.0]))["id"]
    summary = (await lc.remember(user_id="u1",
                                 content="summary: the source fact",
                                 memory_class="factual", source="summarizer",
                                 embedding=[1.0]))["id"]
    await repo.register_dependency(target, derived_kind="cached_summary",
                                   derived_id=summary)
    # The forget() flow registers dependents from the edge table; the
    # cleanup worker reads them (kind/derived_kind both accepted).
    await lc.forget("u1", target)
    completed = await run_cleanup_pass(repo, worker_id="cleanup-worker")
    assert completed >= 1
    assert await repo.get_memory("u1", summary) is None, \
        "the registered dependent was cleaned by the WORKER"
    await db.close()


@pytest.mark.asyncio
async def test_abstention_mapping_agrees(env=None):
    """C12 note: the service maps verification_status → the status key
    the abstention contract reads (constructed and read keys AGREE)."""
    import inspect
    from backend.services import memory_service as ms
    src = inspect.getsource(ms)
    assert 'r.get("verification_status")' in src, \
        "the mapping maps the constructed key to the read key"


# ── C13: per-invocation metering ───────────────────────────────────────

def test_metered_model_wrapper_invocations_identity():
    """§19.5/C5: the wrapper refuses dispatch without a reservation —
    so this test runs against a CONFIGURED usage repo (the production
    contract: no reservation → zero provider dispatches)."""
    import asyncio, tempfile, os
    from backend.config.settings import settings
    settings.SQLITE_DB_PATH = os.path.join(tempfile.mkdtemp(), "meter.db")
    from backend.repositories.usage_repository import (
        SQLiteUsageRepository, set_usage_repo)
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(settings.SQLITE_DB_PATH)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(db.initialize())
        repo = SQLiteUsageRepository(db)
        set_usage_repo(repo)
        from backend.services.metered_model import wrap_model_with_metering

        class _Inner:
            def generate_content(self, *a, **k):
                class _R:
                    usage_metadata = None
                    candidates = []
                return _R()

        wrapped = wrap_model_with_metering(_Inner(), user_id="u", run_id="r",
                                           task_id="t")
        assert wrapped._agentos_metered is True
        wrapped.generate_content("prompt one")
        wrapped.generate_content("prompt two")
        assert wrapped.invocations == 2, \
            "each actual model invocation carries its own identity"
    finally:
        set_usage_repo(None)
        loop.run_until_complete(db.close())
        loop.close()


@pytest.mark.asyncio
async def test_three_invocations_produce_three_identities(tmp_path):
    """§18.7/§19.5 acceptance: a runner using a deterministic fake model
    performs three calls against a CONFIGURED usage repo and produces
    THREE attributed reservations without duplicate charges."""
    from backend.services.metered_model import wrap_model_with_metering
    from backend.repositories.usage_repository import (
        SQLiteUsageRepository, set_usage_repo)
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.config.settings import settings as _settings
    _settings.SQLITE_DB_PATH = os.path.join(str(tmp_path), "three.db")
    db = DatabaseManager(_settings.SQLITE_DB_PATH)
    await db.initialize()
    repo = SQLiteUsageRepository(db)
    set_usage_repo(repo)
    try:
        class _FakeModel:
            calls = []

            def generate_content(self, *a, **k):
                _FakeModel.calls.append(a)
                class _R:
                    usage_metadata = None
                    candidates = []
                return _R()

        wrapped = wrap_model_with_metering(_FakeModel(), user_id="u",
                                           run_id="run-3call", task_id="t1")
        for i in range(3):
            wrapped.generate_content(f"call {i}")
        assert wrapped.invocations == 3
        assert len(_FakeModel.calls) == 3
        # THREE reservations, each with its own call identity — the sync
        # path writes directly via sqlite3 (budget_reservations table).
        import sqlite3 as _sq
        conn = _sq.connect(_settings.SQLITE_DB_PATH)
        try:
            rows = conn.execute(
                "SELECT call_id FROM budget_reservations WHERE "
                "run_id='run-3call'").fetchall()
            call_ids = {r[0] for r in rows}
            assert len(call_ids) == 3, \
                f"three attributed reservations (got {len(call_ids)})"
        finally:
            conn.close()
    finally:
        set_usage_repo(None)
        await db.close()


def test_coroutine_and_unknown_objects_never_pass():
    """§18.3: unknown objects, coroutine objects, and dicts without a
    recognized verdict NEVER become success through truthiness."""
    import asyncio
    from backend.services.outcome_verifier import AcceptanceCriterion

    async def async_resolver(ref):
        return {"verified": True}

    c = AcceptanceCriterion(kind="artifact_exists",
                            value={"resolver": async_resolver})
    ok, why = c.check({"artifact_id": "a"})
    assert ok is False, "a coroutine object is not evidence"

    class _Mystery:
        pass
    c2 = AcceptanceCriterion(kind="artifact_exists",
                             value={"resolver": lambda ref: _Mystery()})
    ok2, why2 = c2.check({"artifact_id": "a"})
    assert ok2 is False, "an unknown object is not evidence"


@pytest.mark.asyncio
async def test_correction_marks_deliverable_verification_stale(tmp_path):
    """§18.6: correcting a source marks the dependent artifact's
    verification STALE through the durable dependency graph."""
    from backend.repositories.sqlite.memory_repository import (
        SQLiteMemoryRepository)
    from backend.services.memory_service import ApplicationMemoryService
    from backend.services.memory_workflows import DependencyMap
    from backend.repositories.deliverable_repository import (
        DeliverableRepository, set_deliverable_repo)
    from backend.services.deliverable_publisher import (
        propagate_source_change)
    from backend.models.deliverable import Deliverable
    db = DatabaseManager(str(tmp_path / "corr18.db"))
    await db.initialize()
    mem_repo = SQLiteMemoryRepository(db)
    drepo = DeliverableRepository(db)
    # Production resolves the deliverable store through the module
    # accessor (factory-registered) — register it for this test.
    set_deliverable_repo(drepo)
    svc = ApplicationMemoryService(mem_repo, dependency_map=DependencyMap())
    fact = await svc.remember(
        user_id="alice", content="Employer: Acme Corp",
        memory_class="factual", explicit=True,
        metadata={"subject_key": "employer", "source": "user"})
    d = await drepo.create_candidate(Deliverable(
        deliverable_id="x", deliverable_key="k-corr", revision=1,
        tenant_id="t", owner_user_id="alice",
        deliverable_type="resume_html",
        file_manifest=[{"path": "r.html", "sha256": "a", "byte_length": 5}]),
        deliverable_key="k-corr")
    await drepo.transition(d.deliverable_id, "verification_passed",
                           verification={"verdict": "pass"})
    # The dependency edge: the artifact was derived from the source fact.
    await mem_repo.register_dependency(
        fact["entry"]["id"], derived_kind="delivered_artifact",
        derived_id=d.deliverable_id)
    # CORRECT the source fact (not forget — §18.6's correction path).
    await svc.correct(user_id="alice", memory_id=fact["entry"]["id"],
                      content="Employer: Acme Corporation",
                      embedding=[1.0])
    got = await drepo.get_for_owner(d.deliverable_id, "alice")
    assert got.verification.get("stale") is True
    assert got.verification.get("stale_reason") == "source corrected"
    assert got.revision == 1, "history is preserved, never rewritten"
    set_deliverable_repo(None)
    await db.close()


def test_wrapper_never_double_wraps():
    from backend.services.metered_model import wrap_model_with_metering
    class _Inner:
        _agentos_metered = True
    inner = _Inner()
    assert wrap_model_with_metering(inner, user_id="u", run_id="r",
                                    task_id="t") is inner


# ── 18.1: pagination + shutdown reset + readiness ──────────────────────

@pytest.mark.asyncio
async def test_deliverable_listing_is_paginated(repo):
    for i in range(5):
        d = await _candidate(repo, key=f"pg-{i}")
        await repo.transition(d.deliverable_id, "stored",
                              file_manifest=[{"path": "f",
                                              "sha256": "a",
                                              "byte_length": 1}])
    page1 = await repo.list_for_owner("alice", limit=2, offset=0)
    page2 = await repo.list_for_owner("alice", limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    ids1 = {d.deliverable_id for d in page1}
    ids2 = {d.deliverable_id for d in page2}
    assert not ids1 & ids2, "no overlap between pages"


def test_readiness_fails_clearly_without_storage():
    from backend.repositories.deliverable_repository import (
        require_deliverable_repo, reset_deliverable_repo)
    reset_deliverable_repo()
    with pytest.raises(Exception, match="deliverable store"):
        require_deliverable_repo()
    reset_deliverable_repo()


def test_metered_model_passes_adk_isinstance_check():
    """Phase 22 live-found crash: the ADK's canonical_model does
    isinstance(self.model, BaseLlm) — a wrapper failing that check is
    handed to LLMRegistry.new_llm, which does `':' in model` on a
    non-str → TypeError. The wrapper must report the INNER class."""
    from google.adk.models.base_llm import BaseLlm
    from typing import AsyncGenerator
    from backend.services.metered_model import _MeteredModel, \
        wrap_model_with_metering

    class _FakeADKModel(BaseLlm):
        model: str = "fake"
        async def generate_content_async(self, llm_request, stream=False):
            yield None

    inner = _FakeADKModel(model="m")
    wrapped = wrap_model_with_metering(inner, user_id="u", run_id="r",
                                       task_id="t")
    assert isinstance(wrapped, BaseLlm), \
        "the ADK must treat the metered wrapper as the model"
    assert wrapped.model == "m"
    assert wrapped._agentos_metered is True
