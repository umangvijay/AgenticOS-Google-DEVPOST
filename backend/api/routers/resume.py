import logging
import asyncio
from typing import Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel

from backend.api.dependencies.auth import get_current_user, AuthenticatedUser, require_not_viewer
from backend.security.rate_limiter import check_rate_limit, check_shared_rate_limit
from backend.models.resume import Resume
from backend.services.jd_parser_service import JDParserService
from backend.services.ats_analyzer_service import ATSAnalyzerService
from backend.services.resume_tailor_service import ResumeTailorService

logger = logging.getLogger(__name__)

async def _resume_context(request: Request, user: AuthenticatedUser = Depends(require_not_viewer)):
    from backend.services.llm_context import load_user_llm_keys, load_user_model
    factory = request.app.state.factory
    await load_user_llm_keys(factory.secrets_repo, user.user_id)
    await load_user_model(factory.settings_repo, user.user_id)


router = APIRouter(prefix="/resume", tags=["resume"], dependencies=[Depends(_resume_context)])

class AnalyzeRequest(BaseModel):
    master_resume: Dict[str, Any]
    job_description: str

class TailorRequest(BaseModel):
    master_resume: Dict[str, Any]
    job_description: str
    target_job_id: str

from fastapi import UploadFile, File, Form
import pdfplumber
import io

@router.post("/scan", status_code=status.HTTP_200_OK)
async def scan_resume(
    file: UploadFile = File(...),
    jobDescription: str = Form(...),
    user: AuthenticatedUser = Depends(require_not_viewer)
):
    """
    Parse a PDF resume and analyze it against a job description using Gemini.
    """
    check_rate_limit(f"user:{user.user_id}", "resume_analyze")
    # Phase 3 §3.10: resume work is enforced shared across replicas (the
    # resume_analyze/resume_tailor aliases resolve to resume_generate).
    await check_shared_rate_limit(f"user:{user.user_id}", group="resume_analyze")

    try:
        # Extract text from PDF (filename may be absent — don't crash on it)
        text = ""
        content = await file.read()
        filename = (file.filename or "").lower()
        if filename.endswith(".pdf") or content[:4] == b"%PDF":
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page in pdf.pages:
                    extracted = page.extract_text()
                    if extracted:
                        text += extracted + "\n"
        else:
            text = content.decode('utf-8')

        if not text.strip():
            raise HTTPException(status_code=400, detail="Could not extract text from the file.")

        # Analyze with Gemini (Flash fallbacks + Vertex region from settings).
        # The analyzer is intermittently flaky upstream (audit R2.6.3), so
        # transient model errors get one internal retry before surfacing.
        from backend.services import gemini_client

        prompt = f"""
You are an expert ATS (Applicant Tracking System) Analyzer.
Analyze the following resume against the job description.

JOB DESCRIPTION:
{jobDescription}

RESUME:
{text}

Provide your analysis in JSON format with exactly these keys:
- "score": an integer from 0 to 100 representing the ATS match score.
- "keywords_found": a list of strings of important keywords found in both.
- "keywords_missing": a list of strings of important keywords from the JD missing in the resume.
- "suggestions": a list of strings with actionable advice to improve the resume.

Output ONLY valid JSON.
"""
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                result = await gemini_client.generate_json(prompt)
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0 and gemini_client.is_retryable_model_error(exc):
                    logger.warning("ATS scan model call failed transiently (%s); retrying once", exc)
                    await asyncio.sleep(1.0)
                    continue
                raise
        if not isinstance(result, dict):
            raise HTTPException(status_code=500, detail="Resume analysis returned an unexpected payload.")
        result["extracted_text"] = text[:20000]
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error scanning resume")
        detail = str(e)
        from backend.services.gemini_client import GeminiQuotaExceeded, is_quota_error
        if isinstance(e, GeminiQuotaExceeded) or is_quota_error(e):
            raise HTTPException(
                status_code=429,
                detail="The model quota is exhausted for resume analysis. Add a key in Settings and retry.",
            )
        if isinstance(e, (ValueError, KeyError)) or "pdf" in detail.lower() or "decode" in detail.lower():
            raise HTTPException(
                status_code=400,
                detail="Could not read the resume file. Upload a text-based PDF or plain text.",
            )
        raise HTTPException(
            status_code=502,
            detail="Resume analysis failed upstream. Please retry; if it persists, check model configuration.",
        )

class CreateResumeRequest(BaseModel):
    profile_text: str
    job_description: str = ""
    tailor: bool = False


@router.post("/create", status_code=status.HTTP_200_OK)
async def create_resume(
    body: CreateResumeRequest,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Build an ATS-ready resume from free-form text; optionally score/tailor against a JD."""
    check_rate_limit(f"user:{user.user_id}", "resume_analyze")
    # Phase 3 §3.10: resume work is enforced shared across replicas (the
    # resume_analyze/resume_tailor aliases resolve to resume_generate).
    await check_shared_rate_limit(f"user:{user.user_id}", group="resume_analyze")
    try:
        from backend.services.resume_builder import create_and_score
        result = await create_and_score(body.profile_text, body.job_description, tailor=body.tailor)
        # §17.6/§17.7/§17.9 (review gap): the generated HTML records the
        # durable deliverable — staged publication + typed verification —
        # and the payload carries the delivery for the evidence panel.
        try:
            import tempfile as _tempfile
            from pathlib import Path as _Path
            from backend.services.artifact_builder import (
                record_and_publish_deliverable,
            )
            _dest = _Path(_tempfile.mkdtemp(prefix="dlv-"))
            _html = result.get("html") or ""
            _path = _dest / "resume.html"
            _path.write_text(_html, encoding="utf-8")
            delivery = await record_and_publish_deliverable(
                owner_user_id=user.user_id,
                tenant_id=str(getattr(user, "organization_id", None)
                              or user.user_id),
                deliverable_type="resume_html",
                name="tailored-resume",
                dest=_dest, written=["resume.html"],
                lineage={"generator_version": "resume_router",
                         "tailored": bool(body.tailor)})
            result["delivery"] = {
                "deliverable_id": delivery["deliverable_id"],
                "revision": delivery["revision"],
                "verification": delivery["verification"],
            }
        except Exception:
            logger.warning("Deliverable recording failed; the resume "
                           "result is returned without the delivery "
                           "payload", exc_info=True)
        return result
    except Exception as e:
        logger.error(f"Error creating resume: {e}")
        raise HTTPException(status_code=400, detail="Resume processing failed. Check the submitted data and model configuration.")


@router.post("/analyze", status_code=status.HTTP_200_OK)
async def analyze_resume(
    body: AnalyzeRequest,
    user: AuthenticatedUser = Depends(require_not_viewer)
):
    """
    Analyze a master resume against a job description.
    Provides deterministic scoring and gap analysis.
    """
    check_rate_limit(f"user:{user.user_id}", "resume_analyze")
    # Phase 3 §3.10: resume work is enforced shared across replicas (the
    # resume_analyze/resume_tailor aliases resolve to resume_generate).
    await check_shared_rate_limit(f"user:{user.user_id}", group="resume_analyze")
    
    try:
        resume = Resume.model_validate(body.master_resume)
        jd_parser = JDParserService()
        jd = await jd_parser.parse_job_description(body.job_description)
        
        analyzer = ATSAnalyzerService()
        score = analyzer.analyze(resume, jd)
        return score.model_dump()
    except Exception as e:
        logger.error(f"Error analyzing resume: {e}")
        raise HTTPException(status_code=400, detail="Resume processing failed. Check the submitted data and model configuration.")

@router.post("/tailor", status_code=status.HTTP_200_OK)
async def tailor_resume(
    body: TailorRequest,
    user: AuthenticatedUser = Depends(require_not_viewer)
):
    """
    Tailors a master resume to a job description.
    Does NOT invent facts.
    """
    check_rate_limit(f"user:{user.user_id}", "resume_tailor")
    await check_shared_rate_limit(f"user:{user.user_id}", group="resume_tailor")
    
    try:
        resume = Resume.model_validate(body.master_resume)
        jd_parser = JDParserService()
        jd = await jd_parser.parse_job_description(body.job_description)
        
        tailor_service = ResumeTailorService()
        tailored = await tailor_service.tailor(resume, jd, body.target_job_id)
        
        return {
            "status": "success",
            "tailored_resume": tailored.model_dump()
        }
    except Exception as e:
        logger.error(f"Error tailoring resume: {e}")
        raise HTTPException(status_code=400, detail="Resume processing failed. Check the submitted data and model configuration.")


class RenderRequest(BaseModel):
    resume: Dict[str, Any]
    format: str = "html"


@router.post("/render", status_code=status.HTTP_200_OK)
async def render_resume(
    body: RenderRequest,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Render a structured resume to HTML (and PDF when WeasyPrint is available)."""
    check_rate_limit(f"user:{user.user_id}", "resume_analyze")
    # Phase 3 §3.10: resume work is enforced shared across replicas (the
    # resume_analyze/resume_tailor aliases resolve to resume_generate).
    await check_shared_rate_limit(f"user:{user.user_id}", group="resume_analyze")
    try:
        resume = Resume.model_validate(body.resume)
        from backend.services.resume_renderer import ResumeRendererService
        renderer = ResumeRendererService()
        html = renderer.render_html(resume)
        if (body.format or "html").lower() == "pdf":
            try:
                from fastapi.responses import Response
                pdf = renderer.render_pdf(resume)
                return Response(content=pdf, media_type="application/pdf")
            except Exception as exc:
                logger.warning("PDF render unavailable: %s", exc)
                return {"html": html, "pdf_error": "PDF renderer unavailable; HTML is included instead."}
        return {"html": html}
    except Exception as e:
        logger.error("Error rendering resume: %s", e)
        raise HTTPException(status_code=400, detail="Resume processing failed. Check the submitted data and model configuration.")
