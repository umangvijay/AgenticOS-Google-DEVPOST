"""Workflow template API (Phase 21 §21.1/§21.2) — versioned templates
with preview-before-run and side-effect-free dry runs."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/templates", tags=["templates"])


def _svc(request: Request):
    factory = getattr(request.app.state, "factory", None)
    svc = getattr(factory, "workflow_template_service", None) if factory else None
    if svc is None:
        raise HTTPException(status_code=503, detail={
            "code": "TEMPLATES_UNAVAILABLE",
            "message": "Workflow templates are not configured"})
    return svc


class TemplateBody(BaseModel):
    name: str
    definition: dict
    required_capabilities: list = []
    config_schema: dict = {}


class DryRunBody(BaseModel):
    definition: dict


@router.get("")
async def list_templates(request: Request,
                         user: AuthenticatedUser = Depends(get_current_user)):
    rows = await _svc(request).list_templates(user.user_id)
    return {"templates": rows, "count": len(rows)}


@router.get("/{template_id}")
async def get_template(template_id: str, request: Request,
                       version: int = None,
                       user: AuthenticatedUser = Depends(get_current_user)):
    t = await _svc(request).get_template(template_id, version=version)
    if not t or t.get("tenant_id") != user.user_id:
        raise HTTPException(status_code=404, detail="Template not found")
    return t


@router.post("")
async def create_template(body: TemplateBody, request: Request,
                          user: AuthenticatedUser = Depends(get_current_user)):
    t = await _svc(request).create_template(
        tenant_id=user.user_id, name=body.name, definition=body.definition,
        required_capabilities=body.required_capabilities,
        config_schema=body.config_schema, created_by=f"user:{user.user_id}")
    return t


@router.post("/dry-run")
async def dry_run(body: DryRunBody, request: Request,
                  user: AuthenticatedUser = Depends(get_current_user)):
    """§21.2: side-effect-free execution proposal — expected effects and a
    labeled cost ESTIMATE. Nothing executes."""
    from backend.services.workflow_templates import dry_run_workflow
    return dry_run_workflow(body.definition)
