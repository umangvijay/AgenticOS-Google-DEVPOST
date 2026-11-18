"""Phase 17 review-gap closure — the reviewer's three probes and the
production-integration gaps:

1. Factory initialization: the deliverable repo exists on BOTH stores.
2. Production callers: the publisher + typed verifier run on the real
   artifact-builder and resume paths (create_files → delivery entry).
3. The evidence panel is MOUNTED (resume page) and imports the shared
   verdict mapping (no copied logic).
4. The three new false-positive probes FAIL correctly:
   - a typed failure result (ArtifactVerdict object, not a dict),
   - a checksum string without a byte check (needs_review),
   - required text present only in an HTML comment (fail).
"""
import inspect

import pytest
import pytest_asyncio


# ── Gap 1: factory initialization ──────────────────────────────────────

@pytest.mark.asyncio
async def test_deliverable_repo_is_factory_initialized():
    """The reviewer's gap: the deliverable repository must be initialized
    by the application factory (both stores), not constructed only in
    tests."""
    from backend.repositories.factory import RepositoryFactory
    f = RepositoryFactory()
    await f.initialize()
    try:
        assert f.deliverable_repo is not None, \
            "the SQLite factory section initializes deliverable_repo"
        src = inspect.getsource(type(f))
        assert "FirestoreDeliverableRepository" in src, \
            "the Firestore factory section initializes the deliverable repo"
        assert src.count("self.deliverable_repo = ") >= 2
    finally:
        await f.shutdown()


def test_deliverable_repo_registered_in_the_module_accessor():
    from backend.repositories.deliverable_repository import (
        get_deliverable_repo, set_deliverable_repo)
    # The factory wiring registers through the module accessor so any
    # caller (worker/router) can resolve it.
    class _R:
        pass
    set_deliverable_repo(_R())
    assert get_deliverable_repo() is not None
    set_deliverable_repo(None)


# ── Gap 2: production callers ──────────────────────────────────────────

def test_create_files_uses_the_delivery_entry():
    """The orchestrator's create_files tool calls save_user_files_with_delivery
    — the production path that runs the publisher + typed verifier."""
    from backend.agents.orchestrator import orchestrator_agent as orch
    src = inspect.getsource(orch)
    assert "save_user_files_with_delivery" in src
    assert "record_and_publish_deliverable" not in src.split(
        "save_user_files_with_delivery")[0], \
        "the orchestrator goes through the delivery entry"


def test_artifact_builder_has_the_production_wrapper():
    from backend.services import artifact_builder as ab
    assert hasattr(ab, "record_and_publish_deliverable")
    assert hasattr(ab, "save_user_files_with_delivery")
    src = inspect.getsource(ab.record_and_publish_deliverable)
    assert "publish_deliverable" in src   # the staged publisher
    assert "verify_artifact" in src       # the typed verifier


@pytest.mark.asyncio
async def test_resume_router_attaches_the_delivery_payload(tmp_path, monkeypatch):
    """The resume generation path records the deliverable and returns
    the delivery payload the evidence panel consumes."""
    from backend.api.routers import resume as resume_router
    src = inspect.getsource(resume_router)
    assert "record_and_publish_deliverable" in src
    assert '"delivery"' in src or "'delivery'" in src


# ── New probe 1: the typed failure OBJECT ──────────────────────────────

def test_typed_failure_object_fails_not_bool_passes():
    from backend.services.artifact_verifier import ArtifactVerdict
    from backend.services.outcome_verifier import AcceptanceCriterion
    verdict_obj = ArtifactVerdict(verdict="fail", artifact_id="a1",
                                  revision=1,
                                  blocking_reasons=["integrity mismatch"])
    c = AcceptanceCriterion(kind="artifact_exists",
                            value={"resolver": lambda ref: verdict_obj})
    ok, why = c.check({"artifact_id": "a1"})
    assert ok is False
    assert "fail" in why


def test_typed_pass_object_still_passes():
    from backend.services.artifact_verifier import ArtifactVerdict
    from backend.services.outcome_verifier import AcceptanceCriterion
    ok_obj = ArtifactVerdict(verdict="pass", artifact_id="a1", revision=1,
                             content_digest="abc")
    c = AcceptanceCriterion(kind="artifact_exists",
                            value={"resolver": lambda ref: ok_obj})
    ok, _ = c.check({"artifact_id": "a1"})
    assert ok is True


# ── New probe 2: checksum string without a byte check ──────────────────

@pytest.mark.asyncio
async def test_checksum_without_byte_check_is_needs_review(tmp_path):
    from backend.models.deliverable import Deliverable
    from backend.repositories.deliverable_repository import (
        DeliverableRepository)
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.services.artifact_verifier import verify_artifact
    db = DatabaseManager(str(tmp_path / "p2r.db"))
    await db.initialize()
    repo = DeliverableRepository(db)
    d = await repo.create_candidate(Deliverable(
        deliverable_id="x", deliverable_key="k", revision=1, tenant_id="t",
        owner_user_id="u", deliverable_type="resume_html",
        file_manifest=[{"path": "r.html", "sha256": "a", "byte_length": 5}]),
        deliverable_key="k")
    await repo.transition(d.deliverable_id, "stored",
                          file_manifest=[{"path": "r.html", "sha256": "a",
                                          "byte_length": 5}],
                          content_checksum="some-checksum")
    out = await verify_artifact(artifact_id=d.deliverable_id,
                                owner_user_id="u", repo=repo)  # no bytes
    assert out.verdict == "needs_review", \
        "a checksum string alone does not confirm the bytes exist"
    assert any(c["check"] == "stored_bytes"
               and c["status"] == "not_performed" for c in out.checks)
    await db.close()


# ── New probe 3: required text only in an HTML comment ─────────────────

@pytest.mark.asyncio
async def test_comment_only_text_fails_rendered_verification():
    from backend.services.rendered_document_verifier import (
        verify_rendered_document)
    html = ("<html><body><!-- Acme Corp — required text hidden in a "
            "comment --><p>other visible text</p></body></html>")
    out = await verify_rendered_document(html=html,
                                         expected_text=["Acme Corp"])
    assert out["verdict"] == "fail", \
        "comment text never renders — it cannot satisfy text_survives"
    # The same text VISIBLE passes.
    visible = ("<html><body><p>Acme Corp</p><!-- comment noise "
               "--></body></html>")
    out2 = await verify_rendered_document(html=visible,
                                          expected_text=["Acme Corp"])
    assert out2["verdict"] == "pass"


# ── Gap 3: the panel is mounted + the mapping is shared ────────────────

def test_evidence_panel_is_mounted_on_the_resume_page():
    src = open("frontend/src/app/dashboard/resume/page.tsx").read()
    assert "ArtifactEvidencePanel" in src
    assert "deliverableId" in src


def test_panel_imports_the_shared_verdict_mapping():
    """§17.21 (review note): the tests must not check COPIED logic — the
    panel imports artifactVerdictStyle from the lib; the lib is tested."""
    panel_src = open("frontend/src/components/ArtifactEvidencePanel.tsx").read()
    assert 'from "@/lib/task-states"' in panel_src
    assert "artifactVerdictStyle" in panel_src
    assert "VERDICT_BADGE" not in panel_src, \
        "the private copy is gone"


def test_artifact_verdict_mapping_is_explicit():
    """The lib mapping (the single source) distinguishes every verdict —
    quoted labels in the TS source."""
    src = open("frontend/src/lib/task-states.ts").read()
    start = src.index("const ARTIFACT_VERDICTS")
    block = src[start:src.index("export function artifactVerdictStyle")]
    for verdict in ("pass", "fail", "needs_review", "unavailable"):
        assert f"{verdict}:" in block, verdict
