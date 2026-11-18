"""
AgentOS — Settings Router

GET  /api/v1/settings — Get user settings
PUT  /api/v1/settings — Update user settings
"""

import logging

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, Field
from typing import Optional

from backend.api.dependencies.auth import get_current_user, AuthenticatedUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


def _get_factory(request: Request):
    factory = getattr(request.app.state, "factory", None)
    if not factory:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return factory


class SettingsUpdate(BaseModel):
    theme: Optional[str] = None                    # "dark" | "light"
    autonomy_level: Optional[int] = None           # 0-3
    default_model: Optional[str] = None            # validated Gemini model ID
    notifications_enabled: Optional[bool] = None
    daily_token_limit: Optional[int] = None
    auto_approve_low_risk: Optional[bool] = None
    webhook_url: Optional[str] = None              # outbound run-finished webhook (public HTTPS)


@router.get("")
async def get_settings(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Get the current user's settings."""
    factory = _get_factory(request)
    settings = await factory.settings_repo.get_settings(user.user_id)
    from backend.services.gemini_client import allowed_models
    return {"settings": settings, "available_models": allowed_models()}


@router.put("")
async def update_settings(
    body: SettingsUpdate,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Update user settings (merge semantics)."""
    factory = _get_factory(request)

    # Build updates dict from non-None fields
    updates = {}
    for field, value in body.model_dump().items():
        if value is not None:
            # Validate specific fields
            if field == "autonomy_level" and not (0 <= value <= 3):
                raise HTTPException(status_code=400, detail="Autonomy level must be 0-3")
            if field == "theme" and value not in ("dark", "light"):
                raise HTTPException(status_code=400, detail="Theme must be 'dark' or 'light'")
            if field == "default_model":
                from backend.services.gemini_client import allowed_models
                if value not in allowed_models():
                    raise HTTPException(status_code=400, detail="Model is not enabled on this server")
            if field == "daily_token_limit":
                # Phase 2 (§2.3): 0 = no new paid usage; negative rejected;
                # a user can never raise an administrator-controlled ceiling.
                from backend.services.budget_service import validate_daily_token_limit
                from backend.models.usage import BudgetError
                from backend.config.settings import settings as app_settings
                try:
                    value = validate_daily_token_limit(
                        int(value), platform_ceiling=app_settings.PLATFORM_DAILY_TOKEN_LIMIT,
                    )
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="daily_token_limit must be an integer")
                except BudgetError as exc:
                    raise HTTPException(status_code=400, detail=exc.message)
            if field == "webhook_url":
                value = str(value).strip()
                if value:
                    from backend.mcp.builder.openapi_parser import OpenAPIParser, SSRFViolationError
                    try:
                        OpenAPIParser()._validate_ssrf(value)
                    except SSRFViolationError:
                        raise HTTPException(
                            status_code=400,
                            detail="webhook_url must be a public HTTPS URL",
                        )
                updates[field] = value
                continue
            updates[field] = value

    if not updates:
        raise HTTPException(status_code=400, detail="No settings to update")

    await factory.settings_repo.update_settings(user.user_id, updates)
    settings = await factory.settings_repo.get_settings(user.user_id)

    await factory.audit_repo.log_event({
        "event_type": "SETTINGS_UPDATED",
        "actor_id": user.user_id,
        "actor_type": "USER",
        "resource_id": user.user_id,
        "details": {"updated_fields": list(updates.keys())},
    })

    from backend.services.gemini_client import allowed_models
    return {"settings": settings, "available_models": allowed_models()}


# ── §21.6: per-user provider credentials + readiness ladder ───────────

class ProviderCredentialRequest(BaseModel):
    provider: str = Field(min_length=1)
    credential: str = Field(min_length=1)
    label: str = ""

class ProviderReadinessRequest(BaseModel):
    provider: str = Field(min_length=1)
    depth: str = "credentials"   # none|configured|credentials|model|inference

PROVIDER_SECRET_KEYS = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "bedrock": "AWS_BEARER_TOKEN_BEDROCK",
    "azure": "AZURE_OPENAI_API_KEY",
    "huggingface": "HF_TOKEN",
    "ollama": "",   # local: no credential
}


def _secrets_repo(request: Request):
    factory = getattr(request.app.state, "factory", None)
    return getattr(factory, "secrets_repo", None)


@router.post("/provider-credentials")
async def store_provider_credential(
    body: ProviderCredentialRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Store THIS user's credential for a provider in the vault —
    per-user ownership, never a process-global switch (§21.6)."""
    key = PROVIDER_SECRET_KEYS.get(body.provider)
    if key is None:
        raise HTTPException(status_code=400, detail=f"unknown provider {body.provider!r}")
    if key == "":
        raise HTTPException(status_code=400,
                            detail="ollama is local — no credential needed")
    repo = _secrets_repo(request)
    if repo is None:
        raise HTTPException(status_code=503, detail="credential store unavailable")
    from backend.security.secrets_vault import secrets_vault
    ciphertext = secrets_vault.encrypt(body.credential)
    await repo.store_secret(user.user_id, key, ciphertext)
    return {"stored": True, "provider": body.provider, "state": "configured"}


@router.delete("/provider-credentials/{provider}")
async def revoke_provider_credential(
    provider: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Revocation affects SUBSEQUENT calls only — in-flight calls are
    already dispatched; later readiness probes report not_configured."""
    key = PROVIDER_SECRET_KEYS.get(provider)
    if not key:
        raise HTTPException(status_code=400, detail=f"unknown provider {provider!r}")
    repo = _secrets_repo(request)
    if repo is None:
        raise HTTPException(status_code=503, detail="credential store unavailable")
    await repo.delete_secret(user.user_id, key)
    return {"revoked": True, "provider": provider,
            "effect": "subsequent calls only"}


@router.post("/provider-readiness")
async def provider_readiness_endpoint(
    body: ProviderReadinessRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Configured → Credentials accepted → Model accessible →
    Inference verified — computed against THIS user's credential."""
    from backend.services.provider_readiness import provider_readiness
    repo = _secrets_repo(request)
    result = await provider_readiness(
        user.user_id, body.provider, secrets_repo=repo,
        probe_depth=body.depth)
    return result
