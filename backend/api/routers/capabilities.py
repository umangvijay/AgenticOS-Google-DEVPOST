"""
Direct capability endpoints so any of these jobs can run without waiting on the planner.

POST /api/v1/capabilities/site-health
POST /api/v1/capabilities/debug
POST /api/v1/capabilities/generate
GET  /api/v1/capabilities/generate/{job_id}
"""

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from backend.security.json_safety import safe_json_dumps
from backend.api.dependencies.auth import AuthenticatedUser, require_not_viewer
from backend.config.settings import settings
from backend.security.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/capabilities", tags=["capabilities"])

_JOBS: dict[str, dict] = {}


class SiteHealthRequest(BaseModel):
    url: str


class DebugRequest(BaseModel):
    source: str
    language: str = "python"
    error_message: str = ""
    goal: str = ""


class GenerateRequest(BaseModel):
    brief: str
    kind: str = "website"
    name: str = ""
    scale: str = "standard"


async def _apply_user_key(request, user_id: str) -> None:
    factory = getattr(request.app.state, "factory", None)
    if factory:
        from backend.services.llm_context import load_user_llm_keys
        await load_user_llm_keys(factory.secrets_repo, user_id)
        from backend.services.llm_context import load_user_model
        await load_user_model(factory.settings_repo, user_id)


def _put_job(job: dict) -> None:
    from pathlib import Path
    import json
    import os
    from backend.services.artifact_builder import artifacts_root
    folder = artifacts_root() / "_jobs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{job['job_id']}.json"
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(job), encoding="utf-8")
    os.replace(temp, path)
    _JOBS[job["job_id"]] = dict(job)
    if (settings.STORAGE_BACKEND or "").lower() != "firestore" or not settings.GOOGLE_CLOUD_PROJECT:
        return
    from google.cloud import firestore as fs
    fs.Client(project=settings.GOOGLE_CLOUD_PROJECT).collection("artifact_jobs").document(job["job_id"]).set(job)


def _get_job(job_id: str) -> Optional[dict]:
    if (settings.STORAGE_BACKEND or "").lower() != "firestore" or not settings.GOOGLE_CLOUD_PROJECT:
        if job_id in _JOBS:
            return dict(_JOBS[job_id])
        import json
        from backend.services.artifact_builder import artifacts_root
        try:
            safe_id = str(uuid.UUID(job_id))
            path = artifacts_root() / "_jobs" / f"{safe_id}.json"
            return json.loads(path.read_text()) if path.exists() else None
        except (ValueError, OSError):
            return None
    from google.cloud import firestore as fs
    doc = fs.Client(project=settings.GOOGLE_CLOUD_PROJECT).collection("artifact_jobs").document(job_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    _JOBS[job_id] = data
    return data


@router.post("/site-health")
async def site_health(body: SiteHealthRequest, request: Request, user: AuthenticatedUser = Depends(require_not_viewer)):
    check_rate_limit(f"user:{user.user_id}", "general")
    try:
        from backend.services.website_health import check_website
        return await check_website(body.url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/debug")
async def debug(body: DebugRequest, request: Request, user: AuthenticatedUser = Depends(require_not_viewer)):
    check_rate_limit(f"user:{user.user_id}", "general")
    await _apply_user_key(request, user.user_id)
    try:
        from backend.services.code_debugger import debug_code
        return await debug_code(body.source, body.language, body.error_message, body.goal)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class PingRequest(BaseModel):
    candidate_key: Optional[str] = None


@router.post("/ping")
async def ping_gemini(body: PingRequest, request: Request, user: AuthenticatedUser = Depends(require_not_viewer)):
    """Cheap check that the shared or vault Gemini key can complete a request.
    If candidate_key is provided, test that key instead of the vault/server key.
    """
    check_rate_limit(f"user:{user.user_id}", "general")

    if body.candidate_key and body.candidate_key.strip():
        from backend.services import gemini_client
        from backend.services.llm_context import set_user_gemini_key, get_user_gemini_key, load_user_model
        factory = getattr(request.app.state, "factory", None)
        await load_user_model(getattr(factory, "settings_repo", None), user.user_id)
        previous = get_user_gemini_key()
        set_user_gemini_key(body.candidate_key)
        try:
            reply = await gemini_client.generate_text("Reply with exactly: PONG")
            return {"ok": "PONG" in (reply or "").upper(), "reply": (reply or "")[:80], "using_user_key": True}
        except Exception:
            logger.exception("Candidate model credential test failed")
            raise HTTPException(status_code=400, detail="Model connection failed. Check the key, selected model, and account quota.")
        finally:
            set_user_gemini_key(previous)

    # Default: test the stored vault key or server key
    await _apply_user_key(request, user.user_id)
    try:
        from backend.services import gemini_client
        from backend.services.llm_context import get_user_gemini_key
        reply = await gemini_client.generate_text("Reply with exactly: PONG")
        return {
            "ok": "PONG" in (reply or "").upper(),
            "reply": (reply or "")[:80],
            "using_user_key": bool(get_user_gemini_key()),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _run_generate_job(job_id: str, user_id: str, brief: str, kind: str, name: str, scale: str) -> None:
    job = await asyncio.to_thread(_get_job, job_id) or {"job_id": job_id, "user_id": user_id}
    job["status"] = "running"
    await asyncio.to_thread(_put_job, job)
    try:
        from backend.services.artifact_builder import generate_project
        async def progress(update):
            job.update(update)
            await asyncio.to_thread(_put_job, dict(job))
        result = await generate_project(user_id, brief, kind=kind, name=name, scale=scale, on_progress=progress)
        job.update({"status": "completed", "result": result, "error": None})
    except Exception as e:
        logger.exception("Studio generate job %s failed", job_id)
        job.update({"status": "failed", "error": "Generation failed. Check model configuration and retry.", "result": None})
    await asyncio.to_thread(_put_job, job)


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate(body: GenerateRequest, request: Request, user: AuthenticatedUser = Depends(require_not_viewer)):
    # Phase 3 §3.10: studio generation is a policy group of its own (the
    # pre-Phase-3 "general" fallback hid its allowance behind the read limit),
    # enforced shared across replicas.
    check_rate_limit(f"user:{user.user_id}", "capabilities")
    from backend.security.rate_limiter import check_shared_rate_limit
    await check_shared_rate_limit(f"user:{user.user_id}", group="capabilities")
    await _apply_user_key(request, user.user_id)
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "user_id": user.user_id,
        "status": "queued",
        "kind": body.kind,
        "scale": body.scale,
        "result": None,
        "error": None,
    }
    await asyncio.to_thread(_put_job, job)
    asyncio.create_task(_run_generate_job(job_id, user.user_id, body.brief, body.kind, body.name, body.scale))
    return {"job_id": job_id, "status": "queued"}


@router.get("/generate/{job_id}")
async def generate_status(job_id: str, user: AuthenticatedUser = Depends(require_not_viewer)):
    job = await asyncio.to_thread(_get_job, job_id)
    if not job or job.get("user_id") != user.user_id:
        raise HTTPException(status_code=404, detail="Generate job not found")
    return job


@router.get("/generate/{job_id}/events")
async def generate_events(job_id: str, request: Request, user: AuthenticatedUser = Depends(require_not_viewer)):
    import json
    from fastapi.responses import StreamingResponse
    await generate_status(job_id, user)
    async def stream():
        previous = None
        while not await request.is_disconnected():
            job = await generate_status(job_id, user)
            payload = safe_json_dumps(job)
            if payload != previous:
                yield f"event: progress\ndata: {payload}\n\n"
                previous = payload
            else:
                yield ": heartbeat\n\n"
            if job.get("status") in ("completed", "failed"):
                return
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})
