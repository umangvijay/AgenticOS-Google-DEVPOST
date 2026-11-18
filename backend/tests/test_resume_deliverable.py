"""Audit G08 acceptance: the resume deliverable is a validated artifact.

- The rendered HTML opens as a document and contains the user's actual
  facts (employers, institutions, skills) — nothing invented.
- The HTML is structurally sound (doctype, closed containers, no template
  placeholders leaking).
- The ATS score carried on results is explicitly the heuristic.
"""
import pytest

from backend.models.resume import Resume
from backend.services.resume_builder import verify_resume_provenance


RESUME = Resume.model_validate({
    "id": "res-1",
    "contact": {"name": "Ada Lovelace", "email": "ada@example.com"},
    "summary": "Analytical engineer.",
    "skills": ["Python", "Analytical Engines"],
    "experience": [{"company": "Difference Engine Co", "title": "Principal",
                    "location": "London", "start_date": "1842", "end_date": "1852",
                    "bullets": ["Wrote the first algorithm."]}],
    "education": [{"institution": "Analytical College", "degree": "BSc Mathematics",
                   "graduation_date": "1841"}],
    "projects": [],
})


def test_g08_rendered_html_opens_and_preserves_user_facts():
    from backend.services.resume_renderer import ResumeRendererService
    html = ResumeRendererService().render_html(RESUME)
    # A document that opens: doctype + basic structure.
    assert html.lstrip().lower().startswith("<!doctype html") or "<html" in html.lower()
    assert "</html>" in html.lower()
    # The user's actual facts are present — nothing dropped.
    for fact in ("Ada Lovelace", "Difference Engine Co", "Analytical College",
                 "BSc Mathematics", "Python", "Wrote the first algorithm."):
        assert fact in html, fact
    # No template placeholder leakage.
    assert "{{" not in html and "%s" not in html


def test_g08_provenance_flags_invented_employer():
    """Adversarial profile: the model slipped in an employer absent from the
    source. The deterministic check must flag it, not let it pass silently."""
    source = "Ada Lovelace worked at Difference Engine Co in London."
    resume = {
        "experience": [
            {"company": "Difference Engine Co", "title": "Principal"},
            {"company": "Imaginary Widgets Ltd", "title": "CTO"},  # invented
        ],
        "education": [{"institution": "Analytical College", "degree": "BSc Mathematics"}],
    }
    report = verify_resume_provenance(source, resume)
    flagged = {item["value"] for item in report["unverified"]}
    assert "Imaginary Widgets Ltd" in flagged
    assert "Difference Engine Co" not in flagged
    assert report["needs_review"] is True
    # The grounded employer is verified, not flagged.
    verified = {item["value"] for item in report["verified"]}
    assert "Difference Engine Co" in verified


def test_g08_ats_score_is_explicitly_the_heuristic():
    from backend.services.ats_analyzer_service import ATSAnalyzerService
    from backend.models.resume import JobDescription
    jd = JobDescription(raw_text="Engineer at C. Python required.", title="Engineer",
                        company="C", required_skills=["Python"], preferred_skills=[],
                        keywords=["python"], required_experience_years=1,
                        education_requirements=["Mathematics"])
    score = ATSAnalyzerService().analyze(RESUME, jd)
    assert score.score_kind == "heuristic"
    # §17.16: the note names the heuristic and what it is NOT —
    # hiring probability, third-party certification, factual proof.
    assert "NOT a hiring probability" in score.score_note
    assert "NOT a third-party ATS certification" in score.score_note
