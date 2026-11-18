"""Phase 17 Batch D acceptance (§17.9–§17.13; findings E + F): the four
reproduced false positives are CLOSED, and tailoring reverifies."""
import pytest

from backend.services.artifact_verifier import ArtifactVerdict, verify_artifact
from backend.services.deliverable_facts import check_resume_facts
from backend.services.outcome_verifier import AcceptanceCriterion
from backend.services.provenance import track_provenance


# ── False positive 1: changed employer under an existing key ───────────

def test_changed_employer_under_same_key_is_flagged():
    out = check_resume_facts(
        supplied={"employer": "Acme"},
        draft={"employer": "Invented Corp"})
    assert out["grounded"] is False, \
        "key presence never proved support — the VALUE must agree"
    (flag,) = out["unsupported_additions"]
    assert flag["reason"].startswith("CONTRADICTION")


# ── False positive 2: self-declared source_fact without evidence ───────

def test_self_declared_source_fact_is_unbacked():
    out = check_resume_facts.__class__ and None or None
    from backend.services.deliverable_facts import check_report_sources
    out = check_report_sources(
        claims=[{"claim_id": "c1", "kind": "source_fact",
                 "fact_id": "fact-never-registered"}],
        verified_fact_ids=[])
    assert out["grounded"] is False
    assert out["classification"]["unbacked"] == ["c1"], \
        "a label cannot vouch for itself"


# ── False positive 3: 10% → 90% numeric contradiction ──────────────────

def test_numeric_substitution_is_a_contradiction():
    report = track_provenance(
        {"summary": "revenue increased 10% year over year"},
        {"source1": "the company reported that revenue increased 10% "
                    "year over year"})
    (fact,) = report.facts
    assert fact.supported is True   # the faithful number is supported
    report2 = track_provenance(
        {"summary": "revenue increased 90% year over year"},
        {"source1": "the company reported that revenue increased 10% "
                    "year over year"})
    (fact2,) = report2.facts
    assert fact2.supported is False, \
        "a changed quantity is a contradiction, not a paraphrase"


def test_negation_flip_is_a_contradiction():
    report = track_provenance(
        {"summary": "the candidate is not certified in cloud"},
        {"source1": "the candidate is certified in cloud"})
    (fact,) = report.facts
    assert fact.supported is False


# ── False positive 4: the negative result dictionary ───────────────────

def test_negative_result_dict_fails_not_bool_passes():
    c = AcceptanceCriterion(kind="artifact_exists", value={"resolver": lambda ref: {"verified": False, "reason": "checksum_mismatch"}})
    ok, why = c.check({"artifact_id": "art-1"})
    assert ok is False
    assert "checksum_mismatch" in why


def test_pass_and_unverifiable_verdicts():
    c_pass = AcceptanceCriterion(kind="artifact_exists",
                                 value={"resolver": lambda ref: {"verdict": "pass"}})
    ok, _ = c_pass.check({"artifact_id": "a"})
    assert ok is True
    c_amb = AcceptanceCriterion(kind="artifact_exists",
                                value={"resolver": lambda ref: {"some": "data"}})
    ok2, why2 = c_amb.check({"artifact_id": "a"})
    assert ok2 is False and "UNVERIFIABLE" in why2


# ── §17.9: the typed verifier binds to revision + digest ───────────────

@pytest.mark.asyncio
async def test_typed_verifier_fails_on_integrity_and_ownership(tmp_path):
    from backend.repositories.deliverable_repository import (
        DeliverableRepository)
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "av.db"))
    await db.initialize()
    repo = DeliverableRepository(db)
    import hashlib as _hl
    from backend.models.deliverable import Deliverable
    real_digest = _hl.sha256(b"<html/>").hexdigest()
    d = await repo.create_candidate(Deliverable(
        deliverable_id="x", deliverable_key="k-av", revision=1,
        tenant_id="t", owner_user_id="alice",
        deliverable_type="resume_html",
        file_manifest=[{"path": "r.html", "sha256": real_digest,
                        "byte_length": 7}]))
    await repo.transition(d.deliverable_id, "stored",
                          file_manifest=[{"path": "r.html",
                                          "sha256": real_digest,
                                          "byte_length": 7}],
                          content_checksum="csum-av")
    # Wrong user → unavailable (indistinguishable from missing).
    out = await verify_artifact(artifact_id=d.deliverable_id,
                                owner_user_id="mallory", repo=repo)
    assert out.verdict == "unavailable"
    # Owner + matching bytes → pass.
    async def stored_ok(aid):
        return b"<html/>"
    ok = await verify_artifact(artifact_id=d.deliverable_id,
                               owner_user_id="alice", repo=repo,
                               stored_bytes_fn=stored_ok)
    assert ok.verdict == "pass", \
        "the stored bytes match the manifest digest — verified against " \
        "the exact published bytes"
    # Corrupted stored bytes → FAIL.
    async def stored_bad(aid):
        return b"<html>TAMPERED</html>"
    bad = await verify_artifact(artifact_id=d.deliverable_id,
                                owner_user_id="alice", repo=repo,
                                stored_bytes_fn=stored_bad)
    assert bad.verdict == "fail"
    assert any("does not match its manifest entry" in r
               for r in bad.blocking_reasons), \
        "the per-file contract names the tampered file"
    await db.close()


@pytest.mark.asyncio
async def test_missing_integrity_metadata_needs_review_not_pass(tmp_path):
    """Finding G: legacy records without checksums are needs-review under
    the migration policy — never a silent pass."""
    class LegacyRepo:
        async def get_for_owner(self, aid, owner):
            class R:
                revision = 1
                content_checksum = ""   # legacy: no integrity metadata
            return R()
    out = await verify_artifact(artifact_id="legacy-1",
                                owner_user_id="alice", repo=LegacyRepo())
    assert out.verdict == "needs_review"
    assert any("integrity" in c["check"] for c in out.checks)


# ── Finding F: tailoring reverifies after the material edit ────────────

@pytest.mark.asyncio
async def test_tailoring_recomputes_groundedness(tmp_path, monkeypatch):
    """A tailored résumé with a newly invented employer is flagged even
    when the original passed — provenance/groundedness recompute AFTER
    the edit (finding F)."""
    from unittest.mock import AsyncMock, patch
    from backend.services import resume_builder as rb
    profile = "Employer: Acme Corp 2019-2024 as Engineer. Skills: Python."
    monkeypatch.setattr(rb.ResumeRendererService, "render_html",
                        lambda self, r: "<html>r</html>")
    from backend.models.resume import ContactInfo, Experience, Resume as _R
    _contact = ContactInfo(name="U", email="u@x.io")
    monkeypatch.setattr(rb, "parse_resume_from_text",
                        AsyncMock(return_value=_R(
                            id="r1", contact=_contact,
                            experience=[Experience(
                                company="Acme Corp", title="Engineer",
                                start_date="2019", end_date="2024",
                                bullets=[])],
                            skills=["Python"])))
    # The tailor INVENTS a new employer.
    class _T:
        resume = _R(
            id="r1-t", contact=_contact,
            experience=[Experience(company="Acme Corp", title="Engineer",
                                   start_date="2019", end_date="2024",
                                   bullets=[]),
                        Experience(company="Invented Corp", title="CTO",
                                   start_date="2024", end_date="2025",
                                   bullets=[])],
            skills=["Python"])
    # The tailor's CONSTRUCTOR builds the Gemini client — patch the
    # whole class too (no credentials touched).
    monkeypatch.setattr(
        rb, "ResumeTailorService",
        type("ResumeTailorService", (), {
            "tailor": AsyncMock(return_value=_T())}))
    from backend.models.resume import JobDescription as _JD
    # The JD parser's CONSTRUCTOR builds the Gemini client — patch the
    # whole class so no credentials are touched.
    monkeypatch.setattr(
        rb, "JDParserService",
        type("JDParserService", (), {
            "parse_job_description": AsyncMock(
                return_value=_JD(title="x", company="y", raw_text="x"))}))
    monkeypatch.setattr(rb.ATSAnalyzerService, "analyze",
                        lambda self, r, jd: type("S", (), {
                            "model_dump": lambda self, **kw: {}})())
    out = await rb.create_and_score(profile, job_description="x", tailor=True)
    assert out["tailored"] is True
    g = out["fact_groundedness"]
    assert g["grounded"] is False, \
        "the invented employer is flagged AFTER tailoring"
    assert any("Invented Corp" in str(f.get("value", ""))
               or "Invented Corp" in str(f.get("supplied", ""))
               or f["field"].startswith("experience:Invented")
               for f in g["unsupported_additions"])
