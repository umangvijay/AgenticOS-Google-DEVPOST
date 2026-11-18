"""Prompt version API (Phase 13 §13.5) — admin-gated versioned prompt
management with rollback."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/prompt-versions", tags=["prompts"])


def _store(request: Request):
    factory = getattr(request.app.state, "factory", None)
    store = getattr(factory, "prompt_version_store", None) if factory else None
    if store is None:
        raise HTTPException(status_code=503, detail={
            "code": "PROMPT_STORE_UNAVAILABLE",
            "message": "The versioned prompt store is not configured on this deployment"})
    return store


def _require_admin(user: AuthenticatedUser) -> None:
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")


def _public(v: dict) -> dict:
    return {
        "version_id": v.get("version_id"),
        "prompt_id": v.get("prompt_id"),
        "version": v.get("version"),
        "note": v.get("note", ""),
        "created_by": v.get("created_by"),
        "created_at": v.get("created_at"),
        "template": v.get("template"),
        "activated_by": v.get("activated_by"),
    }


class CheckoutBody(BaseModel):
    prompt_id: str
    template: str
    note: str = ""


class ActivateBody(BaseModel):
    version: int


@router.get("/{prompt_id}/versions")
async def list_versions(prompt_id: str, request: Request,
                        user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    versions = await _store(request).list_versions(prompt_id)
    active = await _store(request).get_active(prompt_id)
    return {"prompt_id": prompt_id,
            "versions": [_public(v) for v in versions],
            "active_version_id": (active or {}).get("version_id")}


@router.get("/{prompt_id}/active")
async def get_active(prompt_id: str, request: Request,
                     user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    active = await _store(request).get_active(prompt_id)
    if not active:
        raise HTTPException(status_code=404, detail="No active version")
    return _public(active)


@router.post("/checkout")
async def checkout(body: CheckoutBody, request: Request,
                   user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    version = await _store(request).checkout(
        body.prompt_id, body.template,
        created_by=f"user:{user.user_id}", note=body.note)
    return _public(version)


@router.post("/{prompt_id}/activate")
async def activate(prompt_id: str, body: ActivateBody, request: Request,
                   user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    try:
        target = await _store(request).get_version(prompt_id, body.version)
        if not target:
            raise HTTPException(status_code=404, detail="Unknown version")
        activated = await _store(request).activate(
            prompt_id, target["version_id"],
            activated_by=f"user:{user.user_id}")
        return _public(activated)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{prompt_id}/rollback/{version}")
async def rollback(prompt_id: str, version: int, request: Request,
                   user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    try:
        rolled = await _store(request).rollback_to(
            prompt_id, version, operator=f"user:{user.user_id}")
        return _public(rolled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
