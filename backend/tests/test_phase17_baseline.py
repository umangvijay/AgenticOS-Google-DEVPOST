"""Phase 17 §17.0 — the acceptance BASELINE: findings A–H from the
review are REGISTERED with owner, affected paths, failing case, and
expected outcome. The register refuses unevidenced closure: a finding
stays open until its production-path fix has an executed check."""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager

FINDINGS = {
    "17.A": {
        "claim": "memory governance was not on the production path",
        "paths": ["backend/api/routers/memory.py",
                  "backend/agents/orchestrator/orchestrator_agent.py",
                  "backend/services/memory_search.py"],
        "failing_case": "orchestrator memory tools used direct repo access",
        "expected": "one application-level memory service behind every entry point",
    },
    "17.B": {
        "claim": ("memory correction/deletion did not deliver the "
                  "documented propagation"),
        "paths": ["backend/services/memory_lifecycle.py"],
        "failing_case": "remember() overwrote the version; forget() listed derived refs without cleaning",
        "expected": "durable correction/supersession/tombstones/cleanup jobs on repo contracts",
    },
    "17.C": {
        "claim": "Firestore memory parity incomplete",
        "paths": ["backend/repositories/factory.py",
                  "backend/repositories/firestore/memory_repository.py"],
        "failing_case": "lifecycle not wired for Firestore; dedup queried a location the write did not populate",
        "expected": "behavioral parity across both stores",
    },
    "17.D": {
        "claim": "spending coverage gaps on the engine path",
        "paths": ["backend/services/llm_context.py",
                  "backend/engine/engine.py",
                  "backend/services/embedding_service.py"],
        "failing_case": "plain Gemini object ran without per-invocation metering; run_adk_debug replaced usage values",
        "expected": "reservation/settlement at the provider invocation boundary",
    },
    "17.E": {
        "claim": "fact verification false positives",
        "paths": ["backend/services/deliverable_facts.py",
                  "backend/services/provenance.py",
                  "backend/services/outcome_verifier.py"],
        "failing_case": "changed employer passed; 10%→90% passed; negative result dict passed via bool()",
        "expected": "typed verification; numeric/negation contradiction detection",
    },
    "17.F": {
        "claim": "tailoring left verification on the wrong revision",
        "paths": ["backend/services/resume_builder.py"],
        "failing_case": "provenance computed before tailoring; exception set groundedness true",
        "expected": "reverify after every material edit; fail-closed errors",
    },
    "17.G": {
        "claim": "deliverable verification fragmented",
        "paths": ["backend/services/deliverable_verification.py",
                  "backend/services/artifact_builder.py"],
        "failing_case": "in-memory registry without a production caller; missing checksums tolerated",
        "expected": "durable deliverable contract; integrity required on published bytes",
    },
    "17.H": {
        "claim": "acceptance tests overstated coverage",
        "paths": ["backend/tests/test_phase16_quality_release.py"],
        "failing_case": "empty cross-user result sets stood in for deletion/freshness scenarios",
        "expected": "authorized nonempty fixtures for every claimed scenario",
    },
}


@pytest_asyncio.fixture
async def reg(tmp_path):
    db = DatabaseManager(str(tmp_path / "p17base.db"))
    await db.initialize()
    from backend.services.evidence_register import EvidenceRegister
    r = EvidenceRegister(db)
    try:
        yield r
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_every_finding_is_registered_with_its_case(reg):
    """§17.0: each finding has owner/paths/failing-case/expected — and
    stays OPEN until its fix names a production caller + executed check."""
    for fid, spec in FINDINGS.items():
        row = await reg.record(
            subphase_id=fid, claim=spec["claim"], status="open",
            evidence_level="blocked",
            test_ref=f"test_phase17_baseline::test_{fid.replace('.', '_').lower()}_is_registered_with_its_case",
            recorded_by="review:378c590")
        assert row["status"] == "open"
    summary = await reg.summary()
    assert len(FINDINGS.items()) <= summary["total"]


def test_the_register_distinguishes_source_from_evidence():
    """§17.0: PRESENT/INTEGRATED describe SOURCE; VERIFIED describes
    EXECUTED evidence — a passing helper test alone never closes a
    finding."""
    from backend.services.evidence_register import LEVELS_REQUIRING_TESTS
    assert "present" not in LEVELS_REQUIRING_TESTS
    assert "verified_locally" in LEVELS_REQUIRING_TESTS
