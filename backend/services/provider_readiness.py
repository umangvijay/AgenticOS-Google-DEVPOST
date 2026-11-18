"""Per-user provider readiness states (§21.6).

The FOUR distinct states, computed per user against their OWN
credentials (never a process-global switch):

    configured           → the user has chosen a provider/kind and stored
                           a credential reference
    credentials_accepted → the provider ACCEPTED the credential (a real
                           probe: catalog/models list, /api/tags, or a
                           Converse validation)
    model_accessible     → the SELECTED model is listed/available
    inference_verified   → a REAL (optionally billable) generation
                           succeeded through the ordinary path

Each level implies the previous; failures are explicit per level.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.config.settings import settings

logger = logging.getLogger("provider_readiness")

STATES = ("configured", "credentials_accepted", "model_accessible",
          "inference_verified")


async def provider_readiness(user_id: str, provider: str, *,
                             settings_repo=None, secrets_repo=None,
                             probe_depth: str = "credentials") -> Dict[str, Any]:
    """Compute the user's readiness ladder for one provider.

    probe_depth: 'none' (configured only), 'credentials' (live probe),
    'model' (also verify the model is listed), 'inference' (a REAL
    minimal generation — billable for hosted providers).
    """
    out: Dict[str, Any] = {
        "provider": provider, "user_id": user_id,
        "state": "not_configured", "detail": "",
        "checks": {},
    }
    if probe_depth == "none":
        return out

    credential = await _user_credential(user_id, provider, secrets_repo)
    if not credential and provider not in ("ollama",):
        out["detail"] = "no credential stored for this user"
        return out
    out["checks"]["configured"] = True
    out["state"] = "configured"

    if probe_depth == "configured":
        return out

    if provider in {"bedrock", "azure"}:
        if probe_depth != "inference":
            out["detail"] = "A scoped inference probe is required to verify this deployment credential"
            return out
        ok, detail = await _verify_inference(provider, credential, _selected_model(provider), user_id)
        out["detail"] = detail
        if ok:
            out["checks"].update(credentials_accepted=True, model_accessible=True, inference_verified=True)
            out["state"] = "inference_verified"
        return out

    accepted, models, detail = await _probe_credential(provider, credential)
    out["checks"]["credentials_accepted"] = accepted
    if not accepted:
        out["detail"] = detail
        return out
    out["state"] = "credentials_accepted"
    out["detail"] = detail

    if probe_depth == "credentials":
        return out

    model = _selected_model(provider)
    if models is not None and model not in models:
        out["detail"] = (f"model {model!r} is not available to this "
                         "credential")
        return out
    out["checks"]["model_accessible"] = True
    out["state"] = "model_accessible"
    if probe_depth == "model":
        return out

    ok, detail = await _verify_inference(provider, credential, model, user_id)
    out["checks"]["inference_verified"] = ok
    if not ok:
        out["detail"] = detail
        return out
    out["state"] = "inference_verified"
    out["detail"] = detail
    return out


async def _user_credential(user_id: str, provider: str,
                           secrets_repo) -> Optional[str]:
    """Read the USER'S OWN credential from the vault — never a global."""
    if secrets_repo is None:
        return None
    names = {
        "gemini": ("GEMINI_API_KEY", "gemini_api_key"),
        "groq": ("GROQ_API_KEY",),
        "bedrock": ("AWS_BEARER_TOKEN_BEDROCK", "bedrock_bearer"),
        "azure": ("AZURE_OPENAI_API_KEY",),
        "huggingface": ("HF_TOKEN",),
    }
    for name in names.get(provider, ()):
        try:
            value = await secrets_repo.get_secret(user_id, name)
        except Exception:
            value = None
        if value:
            from backend.security.secrets_vault import secrets_vault
            return secrets_vault.decrypt(value)
    return None


async def _probe_credential(provider: str, credential: Optional[str]):
    """A REAL credential probe per provider. Returns (accepted, models,
    detail). models is a list when the provider exposes one."""
    import httpx
    try:
        if provider == "gemini":
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": credential})
            if res.status_code == 200:
                names = [m.get("name", "").split("/")[-1]
                         for m in res.json().get("models", [])]
                return True, names, "Google accepted the key (models listed)"
            return False, None, f"Google rejected the key (HTTP {res.status_code})"
        if provider == "groq":
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {credential}"})
            if res.status_code == 200:
                ids = [m.get("id") for m in res.json().get("data", [])]
                return True, ids, "Groq accepted the key (models listed)"
            return False, None, f"Groq rejected the key (HTTP {res.status_code})"
        if provider == "bedrock":
            if credential:
                # Bearer token validation is exercised by the FIRST call;
                # a lightweight Converse validation here would bill.
                return True, None, ("bearer token present — acceptance is "
                                    "verified by the first inference")
            import botocore.session
            await __import__("asyncio").to_thread(
                botocore.session.get_session)
            return True, None, "boto3 credential chain resolvable"
        if provider == "ollama":
            base = settings.OLLAMA_BASE_URL.rstrip("/")
            async with httpx.AsyncClient(timeout=5) as client:
                res = await client.get(f"{base}/api/tags")
            if res.status_code == 200:
                names = [m.get("name") for m in res.json().get("models", [])]
                return True, names, "local Ollama is reachable"
            return False, None, f"Ollama responded HTTP {res.status_code}"
    except Exception as exc:
        logger.warning("provider probe failed (%s): %s", provider,
                       type(exc).__name__)
        return False, None, f"could not reach {provider}"
    return False, None, f"unknown provider {provider!r}"


def _selected_model(provider: str) -> str:
    return {
        "gemini": settings.GEMINI_MODEL,
        "groq": settings.GROQ_MODEL,
        "ollama": settings.OLLAMA_MODEL,
        "bedrock": settings.BEDROCK_MODEL_ID,
        "azure": settings.AZURE_OPENAI_DEPLOYMENT,
        "huggingface": settings.HF_MODEL,
    }.get(provider, "")


async def _verify_inference(provider: str, credential: Optional[str], model: str, user_id: str):
    """Verify the acting user's credential using the ordinary metered dispatch."""
    import asyncio
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    from backend.services import llm_context

    async def run():
        # wait_for schedules this coroutine in its own context. No global
        # provider settings or process-wide accounting handles are replaced.
        llm_context._provider_override.set(provider)
        llm_context.set_user_id(user_id)
        setters = {"gemini": llm_context.set_user_gemini_key, "groq": llm_context.set_user_groq_key,
                   "huggingface": llm_context.set_user_hf_token, "bedrock": llm_context.set_user_bedrock_bearer,
                   "azure": llm_context._user_azure_key.set}
        if provider in setters:
            setters[provider](credential)
        adapter = llm_context.adk_gemini()
        if provider == "gemini":
            from backend.services.metered_model import wrap_model_with_metering
            import uuid
            adapter = wrap_model_with_metering(adapter, user_id=user_id,
                run_id=f"readiness-{uuid.uuid4().hex}", task_id="provider-readiness")
        request = LlmRequest(model=adapter.model, contents=[types.Content(
            role="user", parts=[types.Part(text="Reply with the single word READY")])])
        parts = []
        async for response in adapter.generate_content_async(request):
            if response.content:
                parts.extend(part.text for part in response.content.parts if part.text)
        return "".join(parts).strip()

    try:
        text = await asyncio.wait_for(run(), timeout=120)
    except Exception as exc:
        return False, f"inference failed: {type(exc).__name__}"
    return bool(text), (f"inference verified ({len(text)} chars from {model})" if text else "empty response")
