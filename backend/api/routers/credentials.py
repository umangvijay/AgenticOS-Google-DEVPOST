"""
AgentOS — User Credentials Router

Named, vault-encrypted credentials the agents can use on the user's behalf:
site logins for the browser agent, SMTP settings for email, API keys for
integrations (e.g. Stripe).

POST   /api/v1/credentials          — Store/replace a named credential
GET    /api/v1/credentials          — List credential names (values are never returned)
DELETE /api/v1/credentials/{name}   — Delete a credential

Storage format: the values dict is JSON-encoded, encrypted with AES-256-GCM
via the SecretsVault, and stored under the key "cred:{name}".
"""

import json
import logging
import re
from typing import Dict

from fastapi import APIRouter, HTTPException, Request, Depends, status
from pydantic import BaseModel

from backend.api.dependencies.auth import get_current_user, AuthenticatedUser, require_not_viewer
from backend.security.secrets_vault import secrets_vault
from backend.security.key_envelope import (
    seal_for_credential, open_envelope, is_envelope, decrypt_credential_payload,
)
from backend.security.rate_limiter import check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/credentials", tags=["credentials"])

CRED_PREFIX = "cred:"
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def _get_factory(request: Request):
    factory = getattr(request.app.state, "factory", None)
    if not factory:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return factory


class StoreCredentialRequest(BaseModel):
    name: str                    # e.g. "fluentedge", "smtp", "stripe"
    values: Dict[str, str]       # e.g. {"username": "...", "password": "..."} or {"api_key": "..."}


@router.post("", status_code=status.HTTP_201_CREATED)
async def store_credential(
    body: StoreCredentialRequest, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Encrypt and store a named credential for the current user."""
    check_rate_limit(f"user:{user.user_id}", "general")
    factory = _get_factory(request)

    if not NAME_PATTERN.match(body.name):
        raise HTTPException(
            status_code=400,
            detail="Credential name must be 1-64 chars: letters, digits, dot, dash, underscore",
        )
    if not body.values:
        raise HTTPException(status_code=400, detail="Credential values cannot be empty")
    if len(json.dumps(body.values)) > 16_000:
        raise HTTPException(status_code=400, detail="Credential payload too large")
    # Validate LLM provider keys server-side before storing — reject junk with 400
    if body.name in ("gemini", "grok") and "api_key" in body.values:
        import httpx
        key = body.values["api_key"].strip()
        if not key:
            raise HTTPException(status_code=400, detail="API key cannot be empty")
        if body.name == "gemini":
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",
                        headers={"x-goog-api-key": key},
                        json={"contents": [{"parts": [{"text": "Reply VALID"}]}], "generationConfig": {"maxOutputTokens": 8, "thinkingConfig": {"thinkingLevel": "low"}}},
                    )
                    if resp.status_code == 400 and "API_KEY_INVALID" in resp.text:
                        raise HTTPException(status_code=400, detail="Invalid Gemini API key")
                    if resp.status_code == 403:
                        raise HTTPException(status_code=400, detail="Gemini API key is forbidden — check project restrictions")
                    if resp.status_code not in (200, 429):
                        raise HTTPException(status_code=400, detail=f"Gemini key check failed: HTTP {resp.status_code}")
            except httpx.TimeoutException:
                raise HTTPException(status_code=400, detail="Gemini text generation timed out; the key was not stored because generation access could not be verified")
            except httpx.HTTPError as e:
                # HTTP exceptions can include the query URL containing the API key.
                logger.warning("Gemini credential check failed (%s)", type(e).__name__)
                raise HTTPException(status_code=400, detail="Could not reach Gemini to validate this key")
        elif body.name == "grok":
            if not key.startswith("xai-"):
                raise HTTPException(status_code=400, detail="Grok key must start with 'xai-'")
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        "https://api.x.ai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"model": "grok-3-mini-fast", "messages": [{"role": "user", "content": "VALID"}], "max_tokens": 5},
                    )
                    if resp.status_code == 401:
                        raise HTTPException(status_code=400, detail="Invalid xAI API key")
                    if resp.status_code not in (200, 429):
                        raise HTTPException(status_code=400, detail=f"xAI key check failed: HTTP {resp.status_code}")
            except httpx.HTTPError as e:
                raise HTTPException(status_code=400, detail=f"Could not validate Grok key: {str(e)[:200]}")

    values_json = json.dumps(body.values)
    storage_format = "envelope"
    if getattr(secrets_vault, "use_gcp", False) and getattr(secrets_vault, "gcp_client", None):
        # Managed-secret storage: persist a VERSION-QUALIFIED reference so
        # upstream rotation cannot silently change what this record reads.
        secret_id = f"cred-{user.user_id}-{body.name}"
        try:
            version = secrets_vault.gcp_client.set_secret(secret_id, values_json)
        except Exception:
            raise HTTPException(status_code=503, detail="credential temporarily unavailable")
        encrypted = f"gcp://{secret_id}@{version}" if version else f"gcp://{secret_id}"
        storage_format = "managed-versioned" if version else "managed-latest"
    else:
        # Local storage: tenant/purpose-bound envelope (30.7). The record
        # authenticates owner, credential id and purpose as AEAD
        # associated data — it cannot be opened under another identity.
        encrypted = seal_for_credential(
            values_json, owner_id=user.user_id, name=body.name,
        )
    await factory.secrets_repo.store_secret(user.user_id, f"{CRED_PREFIX}{body.name}", encrypted)

    # §29.5: every stored credential gets a durable binding — the first
    # store creates it, a RE-STORE is a rotation (version + epoch both
    # advance → validation evidence under the previous context is stale).
    binding = None
    try:
        from backend.services.credential_binding_store import (
            CredentialBindingStore)
        binding_store = CredentialBindingStore(factory.db_manager)
        binding = await binding_store.record_store(
            owner_id=user.user_id, name=body.name)
    except Exception:
        logger.warning("Credential binding record failed", exc_info=True)

    await factory.audit_repo.log_event({
        "event_type": "CREDENTIAL_STORED",
        "actor_id": user.user_id, "actor_type": "USER",
        "resource_id": body.name,
        "details": {"fields": sorted(body.values.keys()),  # field names only, never values
                    "rotated": bool(binding and binding.get("rotated"))},
    })

    return {"name": body.name, "fields": sorted(body.values.keys()),
            "stored": True, "storage_format": storage_format,
            "binding": {"binding_id": (binding or {}).get("binding_id"),
                        "secret_version": (binding or {}).get("secret_version"),
                        "authorization_epoch": (binding or {}).get("authorization_epoch"),
                        "rotated": bool((binding or {}).get("rotated"))}}


class ValidateCredentialRequest(BaseModel):
    name: str
    values: Dict[str, str]


@router.post("/validate")
async def validate_credential(
    body: ValidateCredentialRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Validate a credential by testing it against the provider's API.
    Returns { valid: true } or { valid: false, error: "..." }.
    Does NOT store anything — frontend should call this BEFORE store.
    """
    check_rate_limit(f"user:{user.user_id}", "general")
    key = body.values.get("api_key", "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="api_key is required")

    if body.name == "gemini":
        # Validate Gemini key with a real API call
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",
                    headers={"x-goog-api-key": key},
                    json={"contents": [{"parts": [{"text": "Reply with exactly: VALID"}]}], "generationConfig": {"maxOutputTokens": 8, "thinkingConfig": {"thinkingLevel": "low"}}},
                )
                if resp.status_code == 400 and "API_KEY_INVALID" in resp.text:
                    return {"valid": False, "error": "Invalid Gemini API key. Get one from https://aistudio.google.com/apikey"}
                if resp.status_code == 403:
                    return {"valid": False, "error": "API key is forbidden — check project restrictions."}
                if resp.status_code == 429:
                    # Key is valid but rate-limited — still a valid key
                    return {"valid": True}
                if resp.status_code == 200:
                    return {"valid": True}
                return {"valid": False, "error": f"Gemini validation failed with HTTP {resp.status_code}."}
        except httpx.TimeoutException:
            logger.warning("Gemini credential validation timed out")
            return {"valid": False, "error": "Gemini text generation timed out. The key has not been verified for AI runs."}
        except Exception as e:
            logger.warning("Gemini credential validation failed (%s)", type(e).__name__)
            return {"valid": False, "error": "Could not reach Gemini to validate this key."}

    elif body.name == "grok":
        # Validate xAI/Grok key
        import httpx
        if not key.startswith("xai-"):
            return {"valid": False, "error": "Grok key must start with 'xai-'"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": "grok-3-mini-fast", "messages": [{"role": "user", "content": "Reply VALID"}], "max_tokens": 5},
                )
                if resp.status_code == 401:
                    return {"valid": False, "error": "Invalid xAI API key."}
                if resp.status_code == 429:
                    return {"valid": True}  # rate-limited but valid
                if resp.status_code == 200:
                    return {"valid": True}
                return {"valid": False, "error": f"xAI returned HTTP {resp.status_code}"}
        except Exception as e:
            return {"valid": False, "error": f"Could not reach xAI API: {str(e)[:200]}"}

    elif body.name == "claude":
        # Validate Anthropic key with a tiny live completion
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5", "max_tokens": 5, "messages": [{"role": "user", "content": "Reply VALID"}]},
                )
            if resp.status_code == 401:
                return {"valid": False, "error": "Invalid Anthropic API key."}
            if resp.status_code in (200, 429):
                return {"valid": True}
            return {"valid": False, "error": f"Anthropic returned HTTP {resp.status_code}: {resp.text[:150]}"}
        except Exception as e:
            return {"valid": False, "error": f"Could not reach Anthropic: {str(e)[:150]}"}

    elif body.name == "zai":
        # Z.ai GLM — OpenAI-compatible endpoint
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.z.ai/api/paas/v4/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": "glm-4.5-flash", "messages": [{"role": "user", "content": "Reply VALID"}], "max_tokens": 5},
                )
            if resp.status_code == 401:
                return {"valid": False, "error": "Invalid Z.ai API key."}
            if resp.status_code in (200, 429):
                return {"valid": True}
            return {"valid": False, "error": f"Z.ai returned HTTP {resp.status_code}: {resp.text[:150]}"}
        except Exception as e:
            return {"valid": False, "error": f"Could not reach Z.ai: {str(e)[:150]}"}

    elif body.name == "openai":
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
            if resp.status_code == 401:
                return {"valid": False, "error": "Invalid OpenAI API key."}
            if resp.status_code == 200:
                return {"valid": True}
            return {"valid": False, "error": f"OpenAI returned HTTP {resp.status_code}"}
        except Exception as e:
            return {"valid": False, "error": f"Could not reach OpenAI: {str(e)[:150]}"}

    # Unknown credential type — skip validation, just allow
    return {"valid": True}


@router.get("")
async def list_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List the user's credential names. Values are never returned."""
    factory = _get_factory(request)
    keys = await factory.secrets_repo.list_secret_keys(user.user_id)
    names = [k[len(CRED_PREFIX):] for k in keys if k.startswith(CRED_PREFIX)]
    return {"credentials": names, "count": len(names)}


@router.delete("/{name}")
async def delete_credential(
    name: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Delete a named credential. §29.5: the binding is marked REVOKED
    first — evidence under this context stops being current even after
    the secret bytes are gone."""
    factory = _get_factory(request)
    try:
        from backend.services.credential_binding_store import (
            CredentialBindingStore)
        binding_store = CredentialBindingStore(factory.db_manager)
        await binding_store.revoke(user.user_id, name)
    except Exception:
        logger.warning("Credential binding revocation failed", exc_info=True)
    deleted = await factory.secrets_repo.delete_secret(user.user_id, f"{CRED_PREFIX}{name}")
    if not deleted:
        raise HTTPException(status_code=404, detail="Credential not found")

    await factory.audit_repo.log_event({
        "event_type": "CREDENTIAL_DELETED",
        "actor_id": user.user_id, "actor_type": "USER",
        "resource_id": name,
        "details": {"binding_revoked": True},
    })
    return {"name": name, "deleted": True, "binding_revoked": True}


@router.get("/{name}/binding")
async def get_credential_binding(
    name: str, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """§29.5: the durable binding state for one named credential —
    which version/epoch is current, whether it was rotated or revoked."""
    factory = _get_factory(request)
    from backend.services.credential_binding_store import (
        CredentialBindingStore)
    binding_store = CredentialBindingStore(factory.db_manager)
    binding = await binding_store.get_binding(user.user_id, name)
    if not binding:
        raise HTTPException(status_code=404, detail="No binding for credential")
    return {"name": name, "binding_id": binding.get("binding_id"),
            "secret_version": binding.get("secret_version"),
            "authorization_epoch": binding.get("authorization_epoch"),
            "status": binding.get("status"),
            "environment": binding.get("environment"),
            "updated_at": binding.get("updated_at")}


async def load_credential(secrets_repo, user_id: str, name: str) -> Dict[str, str]:
    """Resolve and decrypt a named credential for internal service use.

    Dual reader (30.7): env1 envelopes, version-qualified managed
    references, and legacy vault ciphertext all read through the same
    path, each under its own binding rules."""
    encrypted = await secrets_repo.get_secret(user_id, f"{CRED_PREFIX}{name}")
    if not encrypted:
        raise ValueError(f"No credential named '{name}' is stored")
    if is_envelope(encrypted):
        plaintext = open_envelope(
            encrypted, tenant_id=user_id, credential_id=f"{CRED_PREFIX}{name}",
            purpose="credential-store",
        )
    else:
        plaintext = decrypt_credential_payload(
            encrypted, tenant_id=user_id, credential_id=f"{CRED_PREFIX}{name}",
            purpose="credential-store", vault=secrets_vault,
        )
    return json.loads(plaintext)
