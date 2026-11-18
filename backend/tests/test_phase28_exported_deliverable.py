"""Phase 28 §28.8 — the exported-deliverable verifier.

verify_rendered_document takes HTML; conversion to PDF can change what
the owner actually receives. The exported-file verifier re-opens the
EXPORTED bytes (the real WeasyPrint PDF) and checks that every source
fact survives conversion, the format matches the declared media type,
pages are readable, and the digest binds the verified bytes.
"""
import hashlib

import pytest

from backend.services.rendered_document_verifier import (
    verify_exported_document)


def _resume_html(name: str) -> str:
    return f"""<html><head><title>Resume</title></head>
    <body><h1>{name}</h1>
    <h2>Experience</h2><p>Senior Platform Engineer at Acme Corp — led the
    migration of 42 services to the durable profile.</p>
    <h2>Skills</h2><ul><li>Distributed systems</li><li>Kubernetes</li></ul>
    <p>Valid resumes use accented glyphs — José — and em dashes — yes.</p>
    </body></html>"""


@pytest.fixture
def rendered_pdf():
    from backend.models.resume import ContactInfo, Experience, Resume
    from backend.services.resume_renderer import ResumeRendererService
    resume = Resume(
        id="resume-fixture",
        contact=ContactInfo(name="José Test", email="jose@example.test",
                            phone="+1 555 0100", location="Remote"),
        summary="Platform engineer focused on durable systems.",
        experience=[Experience(
            company="Acme Corp", title="Senior Platform Engineer",
            start_date="2020-01", end_date="Present",
            bullets=["Led the migration of 42 services to the durable "
                     "profile."])],
        skills=["Distributed systems", "Kubernetes"],
    )
    renderer = ResumeRendererService()
    return renderer.render_html(resume), renderer.render_pdf(resume)


@pytest.mark.asyncio
async def test_real_exported_pdf_passes_with_facts_intact(rendered_pdf):
    html, pdf = rendered_pdf
    assert pdf[:4] == b"%PDF" and len(pdf) > 200
    result = await verify_exported_document(
        content=pdf, media_type="application/pdf",
        expected_text=["José Test", "Acme Corp",
                       "migration of 42 services"])
    assert result["verdict"] == "pass", result["blocking_reasons"]
    assert result["pages"] >= 1 and result["blank_pages"] == 0
    assert result["sha256"] == hashlib.sha256(pdf).hexdigest()


@pytest.mark.asyncio
async def test_export_that_loses_a_source_fact_fails(rendered_pdf):
    """The audit's conversion case: if conversion drops a fact the owner
    expects, the exported verdict FAILS — the HTML verdict alone is not
    evidence of what was delivered."""
    _, pdf = rendered_pdf
    result = await verify_exported_document(
        content=pdf, media_type="application/pdf",
        expected_text=["José Test", "a fact the export does not contain"])
    assert result["verdict"] == "fail"
    assert any("fact_survives_export" in b for b in result["blocking_reasons"])


@pytest.mark.asyncio
async def test_mislabeled_or_corrupt_export_fails():
    # Not a PDF at all, declared as one.
    result = await verify_exported_document(
        content=b"<html>pretending</html>", media_type="application/pdf",
        expected_text=["pretending"])
    assert result["verdict"] == "fail"
    assert any("format_matches" in b for b in result["blocking_reasons"])
    # Corrupt PDF bytes.
    corrupt = b"%PDF-1.4 garbage that no parser accepts"
    result = await verify_exported_document(
        content=corrupt, media_type="application/pdf", expected_text=[])
    assert result["verdict"] == "fail"
    assert any("document_parses" in b or "has_pages" in b
               for b in result["blocking_reasons"])
