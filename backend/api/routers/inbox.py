"""Automation inbox API (Phase 21 §21.6) — one actionable surface."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inbox", tags=["inbox"])


@router.get("")
async def automation_inbox(request: Request,
                           user: AuthenticatedUser = Depends(get_current_user)):
    factory = getattr(request.app.state, "factory", None)
    workflow_repo = getattr(factory, "workflow_repo", None) if factory else None
    if workflow_repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "INBOX_UNAVAILABLE",
            "message": "The inbox needs the durable stores"})
    from backend.services.automation_inbox import AutomationInboxService
    svc = AutomationInboxService(
        workflow_repo,
        effect_repo=getattr(factory, "effect_repo", None),
        artifact_repo=getattr(factory, "artifact_repo", None))
    return await svc.collect(user.user_id)
