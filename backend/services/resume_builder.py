"""
AgentOS — Resume builder.

Turns unstructured profile text (or existing JSON) into a structured ATS-ready
resume, scores it against a job description, and renders HTML.
"""

from __future__ import annotations

import uuid
import re
import asyncio
from typing import Any, Dict, Union

from backend.models.resume import Resume
from backend.services import gemini_client
from backend.services.ats_analyzer_service import ATSAnalyzerService
from backend.services.jd_parser_service import JDParserService
from backend.services.resume_renderer import ResumeRendererService
from backend.services.resume_tailor_service import ResumeTailorService


class ResumeBuilderError(Exception):
    pass


def _as_resume(source: Union[str, dict, Resume]) -> Resume:
    if isinstance(source, Resume):
        return source
    if isinstance(source, dict):
        return Resume.model_validate(source)
    text = (source or "").strip()
    if not text:
        raise ResumeBuilderError("Empty resume")
    if text.startswith("{"):
        return Resume.model_validate_json(text)
    raise ResumeBuilderError("Resume text must be parsed first")


def verify_resume_provenance(source_text: str, resume: dict) -> dict:
    """Deterministic grounding check (audit G08): every employer, institution
    and degree the model produced must literally appear in the source text.
    The model is instructed not to invent facts; this check makes any
    invention visible instead of trusting the instruction."""
    source = (source_text or "").casefold()
    source_norm = " ".join(source.split())

    def grounded(value: str) -> bool:
        if not str(value or "").strip():
            return True  # absent fields are not inventions
        needle = " ".join(str(value).casefold().split())
        return needle in source_norm or needle in source

    unverified: list = []
    verified: list = []
    for item in resume.get("experience") or []:
        name = str(item.get("company") or "").strip()
        if not name:
            continue
        (verified if grounded(name) else unverified).append({"kind": "employer", "value": name})
    for item in resume.get("education") or []:
        for key, kind in (("institution", "institution"), ("degree", "degree")):
            value = str(item.get(key) or "").strip()
            if not value:
                continue
            (verified if grounded(value) else unverified).append({"kind": kind, "value": value})
    return {"verified": verified, "unverified": unverified,
            "needs_review": bool(unverified)}


async def parse_resume_from_text(profile_text: str) -> Resume:
    """Convert a CV / notes dump into a structured Resume. Never invent employers."""
    if not (profile_text or "").strip():
        raise ResumeBuilderError("No resume text provided")
    prompt = f"""Convert this person's background into a structured resume JSON.
Do NOT invent employers, dates, degrees, or metrics that are not in the source.
You MAY tighten wording and add ATS-friendly phrasing for facts that ARE present.
If any contact field or date is missing, leave it empty. Never use placeholder or invented contact details.

SOURCE:
{profile_text[:20000]}

Return JSON matching:
{{
  "id": "string",
  "contact": {{"name":"","email":"","phone":"","linkedin":"","github":"","location":""}},
  "summary": "string",
  "skills": ["..."],
  "experience": [{{"company":"","title":"","location":"","start_date":"","end_date":"","bullets":["..."]}}],
  "education": [{{"institution":"","degree":"","graduation_date":""}}],
  "projects": [{{"name":"","description":"","bullets":["..."],"technologies":["..."]}}]
}}
"""
    data = await gemini_client.generate_json(prompt)
    if not isinstance(data, dict):
        raise ResumeBuilderError("Model returned an invalid resume")
    contact = data.setdefault("contact", {})
    source_emails = re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", profile_text, re.I)
    provided = {address.casefold() for address in source_emails}
    if str(contact.get("email") or "").casefold() not in provided:
        contact["email"] = source_emails[0] if source_emails else ""
    data.setdefault("id", str(uuid.uuid4()))
    return Resume.model_validate(data)


async def create_and_score(
    profile_text: str,
    job_description: str = "",
    tailor: bool = False,
) -> Dict[str, Any]:
    resume = await parse_resume_from_text(profile_text)
    renderer = ResumeRendererService()
    resume_dict = resume.model_dump(mode="json")

    def _groundedness(current_dict: Dict[str, Any]) -> Dict[str, Any]:
        """§17.11/§17.13 (finding F): the fact helper's CONTRADICTION
        check decides — the loose word-overlap heuristic is gone, and a
        verifier failure FAILS CLOSED (explicit error, never
        grounded=true)."""
        from backend.services.deliverable_facts import check_resume_facts
        _supplied = {"profile_text": profile_text,
                     "summary": profile_text[:2000]}
        _draft_fields = {
            k: v for k, v in current_dict.items()
            if k in ("summary", "skills") and v}
        for exp in (current_dict.get("experience") or [])[:20]:
            if isinstance(exp, dict) and exp.get("company"):
                _draft_fields[f"experience:{exp.get('company')}"] = \
                    exp.get("title") or ""
        return check_resume_facts(supplied=_supplied, draft=_draft_fields)

    result: Dict[str, Any] = {
        "resume": resume_dict,
        "html": renderer.render_html(resume),
        "provenance": verify_resume_provenance(profile_text, resume_dict),
        "fact_groundedness": _groundedness(resume_dict),
    }
    if not job_description.strip():
        return result

    jd = await JDParserService().parse_job_description(job_description)
    if tailor:
        tailored = await ResumeTailorService().tailor(resume, jd, target_job_id=str(uuid.uuid4()))
        resume = tailored.resume
        result["resume"] = resume.model_dump(mode="json")
        # §17.13 (finding F): tailoring is a MATERIAL edit — the content
        # revision, provenance, groundedness, and rendered output are ALL
        # recomputed AFTER tailoring. A tailored résumé with a newly
        # invented date is flagged even when the original passed.
        result["html"] = renderer.render_html(resume)
        result["tailored"] = True
        result["provenance"] = verify_resume_provenance(
            profile_text, result["resume"])
        result["fact_groundedness"] = _groundedness(result["resume"])
    score = ATSAnalyzerService().analyze(resume, jd)
    result["ats"] = score.model_dump(mode="json")
    result["job"] = {"title": jd.title, "company": jd.company}
    return result


async def score_resume(resume_source: Union[str, dict, Resume], job_description: str) -> Dict[str, Any]:
    resume = _as_resume(resume_source)
    jd = await JDParserService().parse_job_description(job_description)
    score = ATSAnalyzerService().analyze(resume, jd)
    return {
        "ats": score.model_dump(mode="json"),
        "job": {"title": jd.title, "company": jd.company},
        "resume_id": resume.id,
    }
