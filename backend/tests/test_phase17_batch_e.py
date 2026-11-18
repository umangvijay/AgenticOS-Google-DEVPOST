"""Phase 17 Batch E acceptance (§17.14–§17.17): rendered-document
verification, preview isolation, the transparent ATS rubric, and honest
generated-project states."""
import pytest

from backend.services.generated_project_states import (
    award_badges, project_execution_record)
from backend.services.rendered_document_verifier import (
    sanitize_for_rendering, verify_rendered_document)


# ── §17.14: the RENDERED output is verified ────────────────────────────

@pytest.mark.asyncio
async def test_rendered_html_verification_checks():
    html = ("<html><body><h1>Jan Kowalski</h1>"
            "<h2>Experience</h2><p>Engineer at Acme — led 3 projects</p>"
            "<h2>Education</h2><p>BSc café</p>"
            '<a href="https://linkedin.com/in/x">profile</a>'
            "</body></html>")
    out = await verify_rendered_document(
        html=html, expected_text=["Jan Kowalski", "Acme"],
        required_sections=["Experience", "Education"])
    assert out["verdict"] == "pass"
    assert any(c["check"] == "html_parses" for c in out["checks"])


@pytest.mark.asyncio
async def test_lost_text_and_missing_sections_fail():
    out = await verify_rendered_document(
        html="<html><body><p>placeholder</p></body></html>",
        expected_text=["Jan Kowalski"],
        required_sections=["Experience", "Education"])
    assert out["verdict"] == "fail"
    reasons = " ".join(out["blocking_reasons"])
    assert "Jan Kowalski" in reasons and "Experience" in reasons


@pytest.mark.asyncio
async def test_hostile_render_vectors_fail():
    html = ('<html><body><script>fetch("https://evil.test")</script>'
            "<p>ok</p></body></html>")
    out = await verify_rendered_document(html=html, expected_text=["ok"])
    assert out["verdict"] == "fail"
    assert any("no_remote_fetch" in b or "unsafe" in b
               for b in out["blocking_reasons"])


@pytest.mark.asyncio
async def test_disallowed_link_schemes_fail():
    html = '<html><body><a href="javascript:alert(1)">x</a></body></html>'
    out = await verify_rendered_document(html=html, expected_text=["x"])
    assert out["verdict"] == "fail"
    assert any("link_scheme" in b for b in out["blocking_reasons"])


def test_sanitizer_strips_remote_vectors_before_rendering():
    dirty = ('<html><head><style>@import url("https://evil.test/x.css");'
             '</style></head><body><img src="https://evil.test/x.png">'
             "</body></html>")
    clean = sanitize_for_rendering(dirty)
    assert "evil.test" not in clean


# ── §17.15: preview isolation ──────────────────────────────────────────

def test_document_preview_has_no_script_capability():
    """Documents: no script execution by default (sandbox="")."""
    import re
    src = open("frontend/src/app/dashboard/resume/page.tsx").read()
    m = re.search(r'<iframe[^>]*title="Resume preview"[^>]*>', src)
    assert m and 'sandbox=""' in m.group(0), \
        "the resume preview iframe carries an EMPTY sandbox"
    assert "allow-scripts" not in m.group(0)


def test_application_preview_is_script_allowed_origin_denied():
    """Generated applications: scripts run but SAME-ORIGIN stays off —
    preview content cannot read app storage/cookies."""
    import re
    src = open("frontend/src/app/dashboard/studio/page.tsx").read()
    m = re.search(r'sandbox="([^"]*)"', src)
    assert m
    caps = m.group(1)
    assert "allow-scripts" in caps
    assert "allow-same-origin" not in caps, \
        "combining scripts + same-origin undermines the sandbox (MDN)"


# ── §17.16: the ATS rubric is transparent ──────────────────────────────

def test_ats_score_carries_the_rubric_and_disclaimers():
    from backend.models.resume import ATSScore
    s = ATSScore(overall_score=70, skill_match_score=1, keyword_match_score=1,
                 experience_match_score=1, education_match_score=1,
                 completeness_score=1)
    assert s.rubric_version.startswith("agentos-match-heuristic")
    assert abs(sum(s.rubric_weights.values()) - 1.0) < 1e-6
    assert "NOT a hiring probability" in s.score_note
    assert "NOT a third-party ATS certification" in s.score_note
    assert "factual correctness" in s.score_note


def test_ats_service_declares_its_rubric():
    from backend.services.ats_analyzer_service import ATSAnalyzerService
    assert ATSAnalyzerService.RUBRIC_VERSION == "agentos-match-heuristic-2.0"
    assert abs(sum(ATSAnalyzerService.RUBRIC_WEIGHTS.values()) - 1.0) < 1e-6


# ── §17.17: generated projects — honest execution states ───────────────

def test_unexecuted_project_never_gets_badges():
    rec = project_execution_record(
        source_digest="src-1", dependency_lock_digest="lock-1",
        declared_test_scope="none — runner unavailable")
    assert rec["state"] in ("build_not_executed", "execution_unavailable")
    assert award_badges(rec) == [], \
        "an unexecuted project cannot receive a tested/runnable badge"
    assert rec["note"], "the honesty is stated, not implied"


def test_execution_evidence_shape_is_complete():
    rec = project_execution_record(
        source_digest="src-1", dependency_lock_digest="lock-1",
        runner_id="managed-runner", runner_version="1.2.0",
        commands=[{"cmd": "make test", "exit": 0, "log": "all green"}],
        declared_test_scope="unit tests")
    assert rec["commands"][0]["exit"] == 0
    assert rec["runner_version"] == "1.2.0"
