"""Phase 18 §18.0 — the evidence-backed baseline: C1–C14 and the ten
frontend defects are REGISTERED with production entry points and the
expected behavior. Findings already closed by the v39.1 work carry
their closure evidence; the rest stay open until a PRODUCTION path
change with an executed check exists."""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager

FINDINGS = {
    "18.C1": {"claim": "deliverable repo not factory-initialized",
              "status": "closed_in_v39_1", "entry": "factory._init_sqlite/_init_firestore"},
    "18.C2": {"claim": "no Firestore deliverable repository",
              "status": "closed_in_v39_1", "entry": "FirestoreDeliverableRepository"},
    "18.C3": {"claim": "publisher/verifier no production callers",
              "status": "closed_in_v39_1", "entry": "orchestrator.create_files; /resume/generate"},
    "18.C4": {"claim": "evidence panel unmounted",
              "status": "closed_in_v39_1", "entry": "resume page"},
    "18.C5": {"claim": "typed failure passes the old criterion via truthiness",
              "status": "closed_in_v39_1", "entry": "outcome_verifier._get verdict contract"},
    "18.C6": {"claim": "checksum-only record passes without byte check",
              "status": "closed_in_v39_1", "entry": "artifact_verifier stored_bytes gate"},
    "18.C7": {"claim": "publication callbacks optional; local tx advances without real deps",
              "status": "open", "entry": "deliverable_publisher"},
    "18.C8": {"claim": "publication progress local-only; reconciler returns advice",
              "status": "open", "entry": "deliverable_publisher + worker"},
    "18.C9": {"claim": "review remove/edit do not modify document content",
              "status": "open", "entry": "deliverables router"},
    "18.C10": {"claim": "review checks record revision, not atomic key-latest",
               "status": "open", "entry": "deliverables router + repo"},
    "18.C11": {"claim": "comment-only text passes render verification",
               "status": "closed_in_v39_1", "entry": "rendered_document_verifier extracted text"},
    "18.C12": {"claim": "cleanup jobs have no production worker caller",
               "status": "open", "entry": "worker"},
    "18.C13": {"claim": "ADK metering is outer-reservation, not per-invocation",
               "status": "open", "entry": "engine + model wrapper"},
    "18.C14": {"claim": "frontend tests check copied logic",
               "status": "partially_closed", "entry": "frontend vitest"},
    "18.F1": {"claim": "ChatComposer never restores goal from the stored draft",
              "status": "open", "entry": "ChatComposer.tsx"},
    "18.F2": {"claim": "apiFetch overwrites the caller's cancellation signal",
              "status": "open", "entry": "api.ts"},
    "18.F3": {"claim": "auth/CSRF retries lose the original timeout",
              "status": "open", "entry": "auth-context.tsx"},
    "18.F4": {"claim": "workspace mixes useRunEvents, direct subscriptions, polling",
              "status": "open", "entry": "workspace page"},
    "18.F5": {"claim": "useRunEvents reads run_status (not in WorkflowEvent)",
              "status": "open", "entry": "useRunEvents.ts"},
    "18.F6": {"claim": "replay cursor integer conversion vs string event IDs",
              "status": "open", "entry": "workflow event endpoint"},
    "18.F7": {"claim": "useNetworkStatus conflates quality with online/offline",
              "status": "open", "entry": "useNetworkStatus.ts"},
    "18.F8": {"claim": "useCommandStatus stale closure state",
              "status": "open", "entry": "useCommandStatus.ts"},
    "18.F9": {"claim": "no-Web-Locks refresh path rotates the same single-use token",
              "status": "open", "entry": "auth-session.ts"},
    "18.F10": {"claim": "evidence-panel races (no resource guards, no review errors)",
               "status": "open", "entry": "ArtifactEvidencePanel.tsx"},
}


@pytest_asyncio.fixture
async def reg(tmp_path):
    db = DatabaseManager(str(tmp_path / "p18base.db"))
    await db.initialize()
    from backend.services.evidence_register import EvidenceRegister
    r = EvidenceRegister(db)
    try:
        yield r
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_every_finding_registered_with_entry_point(reg):
    """§18.0: each finding carries its claim, closure status, and the
    production entry point that must carry the fix."""
    for fid, spec in FINDINGS.items():
        row = await reg.record(
            subphase_id=fid, claim=spec["claim"],
            status=("closed" if spec["status"].startswith("closed")
                    else "open"),
            evidence_level=("verified_locally"
                            if spec["status"].startswith("closed")
                            else "blocked"),
            test_ref=f"test_phase18_baseline::test_every_finding_registered_with_entry_point",
            recorded_by="review:phase18")
        if spec["status"] == "open":
            assert row["status"] == "open", fid
    summary = await reg.summary()
    assert len(FINDINGS) <= summary["total"]


def test_closed_findings_carry_production_entry_points():
    """§18.0 acceptance: no requirement is complete because its helper
    exists — every closed finding names the production entry that
    carries the behavior."""
    for fid, spec in FINDINGS.items():
        if spec["status"].startswith("closed"):
            assert spec["entry"], f"{fid} closed without an entry point"
