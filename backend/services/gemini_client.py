"""Shared Gemini client — real model calls, config-driven model names."""

import asyncio

import json
from contextlib import asynccontextmanager
import logging
import re
from typing import Any, Dict, Optional, Type

from backend.config.settings import settings
from backend.services.llm_context import effective_gemini_key, effective_groq_key, effective_grok_key, effective_hf_token, get_run_id, get_user_id

logger = logging.getLogger(__name__)


class GeminiQuotaExceeded(RuntimeError):
    """Raised when the configured Gemini key hits RESOURCE_EXHAUSTED / 429."""


def is_quota_error(exc: BaseException) -> bool:
    """True only for quota exhaustion, which may justify a provider fallback."""
    msg = str(exc)
    lowered = msg.lower()
    return any(
        s in msg
        for s in (
            "RESOURCE_EXHAUSTED",
            "quota exhausted",
        )
    ) or "429" in msg or "quota" in lowered


def is_retryable_model_error(exc: BaseException) -> bool:
    """Quota, missing model, or transient Gemini failures — try the next model."""
    # Credentials and project/model permissions are not model-specific. Cycling
    # through every fallback ID only repeats a doomed request and burns latency.
    if is_auth_error(exc):
        return False
    if is_quota_error(exc) or isinstance(exc, GeminiQuotaExceeded):
        return True
    msg = str(exc)
    lowered = msg.lower()
    return any(
        s in msg
        for s in ("NOT_FOUND", "UNAVAILABLE", "overloaded")
    ) or any(
        s in lowered
        for s in ("not found", "not supported", "unknown model", "invalid model")
    )



def is_auth_error(exc: BaseException) -> bool:
    """True when the credential itself is rejected (revoked/expired key)."""
    msg = str(exc)
    return (
        "UNAUTHENTICATED" in msg
        or "API_KEY_INVALID" in msg
        or "PERMISSION_DENIED" in msg
        or "API key not valid" in msg
        or "ACCESS_TOKEN_TYPE_UNSUPPORTED" in msg
        or "401" in msg
    )


async def probe_key_status() -> dict:
    """Classify the configured LLM credential at startup.
    Returns a credential/configuration status. A successful catalog request is
    deliberately not reported as inference-ready; generation is not exercised
    during startup because that can incur cost and delay cold starts.
    """
    import os
    from backend.services.llm_context import active_llm_provider
    provider = active_llm_provider()
    key = (os.environ.get("GEMINI_API_KEY") or settings.GEMINI_API_KEY or "").strip()
    groq_key = (os.environ.get("GROQ_API_KEY") or settings.GROQ_API_KEY or "").strip()
    hf_token = effective_hf_token()
    project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or settings.GOOGLE_CLOUD_PROJECT or "").strip()
    if provider == "groq":
        if not groq_key:
            return {"status": "missing", "detail": "LLM_PROVIDER=groq but GROQ_API_KEY is not configured."}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {groq_key}"},
                )
            if resp.status_code == 200:
                return {"status": "catalog_ok", "detail": "Groq accepted the key for model discovery; inference is verified only by a live agent run."}
            return {"status": "invalid", "detail": f"Groq credential check failed (HTTP {resp.status_code})."}
        except Exception as e:
            logger.warning("Groq credential probe failed (%s)", type(e).__name__)
            return {"status": "unknown", "detail": "Could not reach Groq. Check network access and provider configuration."}
    if provider == "huggingface":
        if not hf_token:
            return {"status": "missing", "detail": "LLM_PROVIDER=huggingface but HF_TOKEN is not configured."}
        return {"status": "configured", "detail": "Hugging Face token is configured; inference is verified only by a live model request."}
    if provider == "gemini" and key:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": key},
                )
            if resp.status_code == 200:
                return {
                    "status": "catalog_ok",
                    "detail": "Key is accepted for model discovery; text generation has not been verified.",
                }
            if resp.status_code in (400, 401, 403):
                return {"status": "invalid", "detail": f"Gemini rejected the configured key (HTTP {resp.status_code}). Add a fresh key from aistudio.google.com/apikey in Settings."}
            return {"status": "invalid", "detail": f"Gemini key check failed (HTTP {resp.status_code})."}
        except Exception as e:
            # HTTP client errors can include endpoint URLs (including query-key
            # credentials) and cloud project details. Expose only the category.
            logger.warning("Gemini credential probe failed (%s)", type(e).__name__)
            return {"status": "unknown", "detail": "Could not reach Gemini. Check network access and provider configuration."}
    if provider == "vertex" and project:
        # Project identifiers are infrastructure details; never expose them from
        # a health/capability response.
        return {"status": "vertex", "detail": "Using Vertex AI (project configured)"}
    return {"status": "missing", "detail": "No GEMINI_API_KEY and no GOOGLE_CLOUD_PROJECT configured. Add a key in Settings."}


FLASH_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
)


def _is_allowed_flash(model: str) -> bool:
    """Flash-tier models used for routine work. Never Pro or Reasoning."""
    name = (model or "").strip().lower()
    return name in FLASH_MODELS


def _is_gemini_35_or_newer(model: str) -> bool:
    return _is_allowed_flash(model)


def allowed_models() -> list[str]:
    from backend.services.llm_context import active_llm_provider
    if active_llm_provider() == "azure":
        return [settings.AZURE_OPENAI_DEPLOYMENT]
    if active_llm_provider() == "huggingface":
        return [settings.HF_MODEL]
    return list(dict.fromkeys(m.strip() for m in settings.GEMINI_ALLOWED_MODELS.split(",") if m.strip()))


def _candidate_models(preferred: Optional[str] = None) -> list:
    """§14.9: the candidate list derives from the ROUTING CATALOG — the
    declared actions with visible availability — so eligibility (§14.10)
    is one filter, not a string-list accident."""
    from backend.services.llm_context import get_user_model
    from backend.services.routing_catalog import (
        RoutingCatalogError, catalog_actions, eligible_actions,
    )
    explicit = preferred or get_user_model()
    actions = catalog_actions(
        allowed_models=allowed_models(),
        primary_model=settings.GEMINI_MODEL,
        flash_models=list(FLASH_MODELS))
    try:
        chosen = eligible_actions(actions, preferred=explicit)
    except RoutingCatalogError as exc:
        raise ValueError(str(exc)) from exc
    return list(dict.fromkeys(chosen))


def candidate_models(preferred: Optional[str] = None) -> list:
    """Models to try for the active shared provider, without retrying an alias as another model."""
    from backend.services.llm_context import active_llm_provider
    if active_llm_provider() == "groq":
        if preferred and preferred != settings.GROQ_MODEL:
            raise ValueError("A Gemini model was selected, but only Groq is configured")
        return [settings.GROQ_MODEL]
    if active_llm_provider() == "huggingface":
        if preferred and preferred != settings.HF_MODEL:
            raise ValueError("A Gemini model was selected, but only Hugging Face is configured")
        return [settings.HF_MODEL]
    return _candidate_models(preferred)


async def run_adk_debug(build_agent, prompt: str, app_name: Optional[str] = None):
    """Run an ADK InMemoryRunner, retrying Flash model IDs on NOT_FOUND/quota.

    Phase 4 §4.0C (R5): every ADK attempt is a PAID provider call and holds
    its own durable reservation (the same contract as the direct API paths).
    Usage metadata from ADK events is captured when present; otherwise the
    estimate-based settlement applies (§2.12 honest-estimation posture)."""
    from google.adk.runners import InMemoryRunner
    from backend.services.budget_service import get_budget_service
    from backend.services.usage_normalizer import estimate_units_from_text
    from backend.services.llm_context import get_run_id, get_user_id

    last: Optional[Exception] = None
    for model in candidate_models():
        service = get_budget_service()
        try:
            async with service.metered_call(
                user_id=get_user_id() or "platform-guest",
                estimated_input_units=estimate_units_from_text(prompt),
                provider="gemini", model=model, purpose="adk_generation",
                run_id=get_run_id(),
            ) as call:
                agent = build_agent(model)
                runner = InMemoryRunner(agent=agent, app_name=app_name or settings.APP_NAME)
                await call.mark_dispatched()
                events = await runner.run_debug(prompt)
                # Best-effort usage capture from ADK event stream.
                text_parts = []
                for evt in events or []:
                    usage = getattr(evt, "usage_metadata", None)
                    if usage is not None:
                        call.capture_usage(usage)
                    content = getattr(evt, "content", None)
                    if content is not None:
                        for part in (getattr(content, "parts", None) or []):
                            t = getattr(part, "text", None)
                            if t:
                                text_parts.append(t)
                if text_parts:
                    call.capture_response_text("".join(text_parts))
                return events
        except Exception as e:
            last = e
            if is_retryable_model_error(e):
                logger.warning("ADK Gemini %s unavailable (%s); trying next Flash model", model, e)
                from backend.services.provider_backoff import provider_backoff
                await provider_backoff(e, attempt=0)
                continue
            raise
    raise last or RuntimeError("ADK runner produced no events")


def _client():
    from google import genai

    from backend.services.llm_context import active_llm_provider
    key = effective_gemini_key()
    if active_llm_provider() != "vertex" and key:
        return genai.Client(api_key=key)
    return genai.Client(
        vertexai=True,
        project=settings.GOOGLE_CLOUD_PROJECT,
        location=settings.GOOGLE_CLOUD_REGION,
    )


async def _generate_content_async(client, **kwargs):
    """Run the sync Gemini SDK off the event loop so workflows can still be polled."""
    return await asyncio.to_thread(client.models.generate_content, **kwargs)


async def _grok_complete(prompt: str, *, json_mode: bool = False, call=None) -> str:
    """xAI OpenAI-compatible chat. Used only after Gemini is unavailable."""
    key = effective_grok_key()
    if not key:
        raise GeminiQuotaExceeded(
            "Gemini quota exhausted and no Grok key is configured. Add a Gemini or xAI key in Settings."
        )
    import httpx

    payload: dict = {
        "model": settings.GROK_MODEL or "grok-4-fast",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1 if json_mode else 0.2,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Grok HTTP {response.status_code}: {response.text[:400]}")
        data = response.json()
    if call is not None:
        from backend.services.usage_normalizer import normalize_openai_usage
        call.capture_usage(normalize_openai_usage(
            data.get("usage"), provider="grok", requested_model=payload["model"],
        ))
    text = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    ).strip()
    if call is not None:
        call.capture_response_text(text)
    if not text:
        raise ValueError("Grok returned an empty response")
    logger.info("LLM used Grok fallback model %s", payload["model"])
    return text


async def _groq_complete(prompt: str, *, json_mode: bool = False, call=None) -> str:
    """Groq OpenAI-compatible fallback. Never include provider response bodies in errors."""
    key = effective_groq_key()
    if not key:
        raise RuntimeError("No Groq API key is configured")
    import httpx

    payload: dict = {
        "model": settings.GROQ_MODEL or "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1 if json_mode else 0.2,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Groq HTTP {response.status_code}")
        data = response.json()
    if call is not None:
        from backend.services.usage_normalizer import normalize_openai_usage
        call.capture_usage(normalize_openai_usage(
            data.get("usage"), provider="groq", requested_model=payload["model"],
        ))
    text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if call is not None:
        call.capture_response_text(text)
    if not text:
        raise ValueError("Groq returned an empty response")
    logger.info("LLM used Groq fallback model %s", payload["model"])
    return text


async def _huggingface_complete(prompt: str, *, json_mode: bool = False, model: Optional[str] = None, call=None) -> str:
    """Call the Hugging Face OpenAI-compatible inference router without logging secrets."""
    key = effective_hf_token()
    if not key:
        raise RuntimeError("No Hugging Face token is configured")
    import httpx

    payload: dict = {
        "model": model or settings.HF_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1 if json_mode else 0.2,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
        response = await client.post(
            "https://router.huggingface.co/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code >= 400:
            if response.status_code == 402:
                raise RuntimeError("Hugging Face inference credits or billing are unavailable (HTTP 402)")
            if response.status_code == 403:
                raise RuntimeError("Hugging Face token lacks Inference Providers access (HTTP 403)")
            if response.status_code == 401:
                raise RuntimeError("Hugging Face rejected the configured token (HTTP 401)")
            raise RuntimeError(f"Hugging Face inference failed (HTTP {response.status_code})")
        data = response.json()
    if call is not None:
        from backend.services.usage_normalizer import normalize_openai_usage
        call.capture_usage(normalize_openai_usage(
            data.get("usage"), provider="huggingface", requested_model=payload["model"],
        ))
    text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if call is not None:
        call.capture_response_text(text)
    if not text:
        raise ValueError("Hugging Face returned an empty response")
    logger.info("LLM used Hugging Face model %s", payload["model"])
    return text


def _groq_is_only_configured_provider() -> bool:
    """True when Groq is the explicitly selected or auto-detected provider."""
    from backend.services.llm_context import active_llm_provider
    return active_llm_provider() == "groq"


def _huggingface_is_only_configured_provider() -> bool:
    from backend.services.llm_context import active_llm_provider
    return active_llm_provider() == "huggingface"


def _ollama_is_only_configured_provider() -> bool:
    """Local inference is a first-class provider on the JSON ladder too
    (Phase 22 §22.3: every AI call path flows through the ordinary
    provider dispatch — never straight to hosted-only code)."""
    from backend.services.llm_context import active_llm_provider
    return active_llm_provider() == "ollama"


async def _adk_prompt(prompt: str) -> str:
    from backend.services.llm_context import adk_gemini
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    adk = adk_gemini()
    req = LlmRequest(model=adk.model, contents=[
        types.Content(role="user", parts=[types.Part(text=prompt)])])
    chunks = []
    async for response in adk.generate_content_async(req):
        chunks.extend(part.text for part in response.content.parts if part.text)
    return "".join(chunks)


async def _ollama_json_complete(prompt: str, *, model: Optional[str] = None,
                                call=None) -> str:
    """Complete a prompt through the METERED OllamaADK path (§2.11 —
    local inference is budgeted exactly like hosted providers)."""
    from backend.services.ollama_adk import OllamaADK
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    adk = OllamaADK(model=model) if model else OllamaADK()
    request = LlmRequest(
        model=getattr(adk, "model", ""),
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
    )
    chunks = []
    async for response in adk.generate_content_async(request):
        chunks.append(response)
    return "".join(p.text or "" for r in chunks
                   for p in (r.content.parts or []))


def _coerce_json(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            text = text[start : end + 1]
    text = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(text, strict=False)



MAX_OUTPUT_RESERVE_TOKENS = 8192  # reserved headroom per paid call

# Per-run token accounting (v5.6 §4): real spend per workflow run, drained
# by the EvaluationService when the run completes — replaces the placeholder
# zero-token evaluation records.
_run_token_usage: Dict[str, int] = {}


def record_run_tokens(run_id: Optional[str], tokens: int) -> None:
    if not run_id or tokens <= 0:
        return
    _run_token_usage[run_id] = _run_token_usage.get(run_id, 0) + int(tokens)


def drain_run_tokens(run_id: str) -> int:
    return _run_token_usage.pop(run_id, 0)


# ── Phase 2 metering (§2.11/§2.20-9) ─────────────────────────────────
# EVERY paid provider attempt runs inside its own durable budget
# reservation: reserve estimated max → dispatch → settle the provider's
# reported usage (or a labeled estimate). Fallbacks and repair calls are
# separate attempts with separate reservations. Consumption is written
# EXACTLY once — by the settlement; the process-local ContextManager and
# per-run counters below are caches, never the authoritative ledger.

GUEST_PAYER = "platform-guest"  # guest runs are paid by the platform (§2.3)


def _is_non_billing_rejection(exc: BaseException) -> bool:
    """Provider refused the request without generating — proven unused."""
    return is_quota_error(exc) or is_auth_error(exc) or is_retryable_model_error(exc)


@asynccontextmanager
async def _metered_attempt(prompt: str, *, provider: str, model: Optional[str],
                           purpose: str = "generation", extra_units: int = 0):
    """One potentially billable provider attempt (§2.11).

    Reserves input estimate + bounded output headroom, commits dispatch
    intent, and settles/uncertains/releases automatically on exit. Usage
    metadata MUST be captured before parsing so application failures still
    settle the model usage (§2.10).

    Phase 3 §3.16 ordering inside this wrapper: (1) provider-throughput
    admission (shared, per credential scope — the user context IS the quota
    owner here), then (2) the Phase 2 spending reservation. A throughput
    refusal raises PROVIDER_THROTTLED before any budget hold is taken, and
    any failure inside releases cleanly.
    """
    from backend.services.budget_service import get_budget_service
    from backend.services.usage_normalizer import estimate_units_from_text

    service = get_budget_service()
    user_id = get_user_id() or GUEST_PAYER
    estimate = estimate_units_from_text(prompt) + int(extra_units)

    from backend.security.rate_limiter import admission_decision
    from backend.models.admission import AdmissionReason, ProviderThrottledError
    from backend.services.llm_context import get_org_id, get_credential_ref
    # §4.17 (C14): quota identity derives from the ACTUAL credential scope.
    # A user-owned credential scopes per credential ref; the SHARED platform
    # credential is ONE quota for all its users — one counter (per provider),
    # never per-user multiplication. Raw credentials never enter keys.
    credential_scope = get_credential_ref() or f"platform:{provider}"
    decision = await admission_decision(f"provider:{provider}:{credential_scope}",
                                        "provider_request")
    if not decision.allowed:
        if decision.reason == AdmissionReason.ADMISSION_UNAVAILABLE:
            # §3.9: consequential provider work requires admission + budget
            # controls — a store outage blocks new paid calls (fail closed).
            raise ProviderThrottledError(
                "Provider admission unavailable; new paid calls are paused",
                retry_after_seconds=decision.retry_after_seconds)
        raise ProviderThrottledError(
            f"Provider throughput limit reached for {provider}",
            retry_after_seconds=decision.retry_after_seconds)

    async with service.metered_call(
        user_id=user_id,
        estimated_input_units=estimate,
        provider=provider,
        model=model,
        purpose=purpose,
        run_id=get_run_id(),
        organization_id=get_org_id(),
    ) as call:
        try:
            yield call
        except BaseException as exc:
            if _is_non_billing_rejection(exc):
                # 429/401/404 rejections never generated: release the hold
                # (stronger evidence than a timeout, §2.13).
                call.note_provider_rejection(type(exc).__name__)
            raise


def _record_process_local(call, prompt: str) -> None:
    """Fast caches only: the process-local ContextManager and per-run token
    counters. The durable ledger was already written by the settlement —
    recording here again would double-count (§2.18)."""
    try:
        user_id = get_user_id()
        if not user_id:
            return
        from backend.agents.context_manager import context_manager
        usage = call.usage
        if usage is not None and usage.known:
            total = usage.policy_units()
            inp = usage.input_tokens
            out = usage.output_tokens
            if inp is None and out is None:
                inp, out = total, 0
        else:
            from backend.services.usage_normalizer import estimate_units_from_text
            total = estimate_units_from_text(prompt, " " * (call.response_text_length))
            inp, out = total, 0
        if inp:
            context_manager.record_usage(user_id, inp, out or 0)
        run_id = get_run_id()
        if run_id:
            record_run_tokens(run_id, total)
    except Exception:
        logger.debug("process-local usage recording failed", exc_info=True)


def _usage_metadata(response, requested_model: Optional[str]):
    from backend.services.usage_normalizer import normalize_gemini_usage
    return normalize_gemini_usage(
        getattr(response, "usage_metadata", None),
        requested_model=requested_model,
        resolved_model=getattr(response, "model_version", None) or requested_model,
    )


async def _generate_text_attempt(prompt: str, model: Optional[str] = None) -> str:
    """Text generation. Every attempt in the candidate loop (and every
    fallback) holds its own durable reservation (§2.11)."""
    from backend.services.llm_context import active_llm_provider
    if active_llm_provider() in ("azure", "bedrock", "ollama"):
        return await _adk_prompt(prompt)
    if _huggingface_is_only_configured_provider():
        async with _metered_attempt(prompt, provider="huggingface",
                                    model=model or settings.HF_MODEL) as call:
            text = await _huggingface_complete(prompt, json_mode=False, model=model, call=call)
        _record_process_local(call, prompt)
        return text
    if not model and _groq_is_only_configured_provider():
        async with _metered_attempt(prompt, provider="groq",
                                    model=settings.GROQ_MODEL) as call:
            text = await _groq_complete(prompt, json_mode=False, call=call)
        _record_process_local(call, prompt)
        return text
    from google.genai import types

    last: Optional[Exception] = None
    quota_hits = 0
    for candidate in _candidate_models(model):
        try:
            async with _metered_attempt(prompt, provider="gemini",
                                        model=candidate) as call:
                client = _client()
                await call.mark_dispatched()
                response = await _generate_content_async(
                    client,
                    model=candidate,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    ),
                )
                call.capture_usage(_usage_metadata(response, candidate))
                text = (response.text or "").strip()
                call.capture_response_text(text)
                if not text:
                    # Empty output still consumed the input; the metered
                    # context settles it and the loop tries another model.
                    last = ValueError(f"empty response from {candidate}")
                    continue
                if candidate != (model or settings.GEMINI_MODEL):
                    logger.info("Gemini text used fallback model %s", candidate)
                _record_process_local(call, prompt)
                return text
        except Exception as e:
            last = e
            if is_retryable_model_error(e):
                logger.warning("Gemini model %s unavailable (%s); trying another", candidate, e)
                if is_quota_error(e) or isinstance(e, GeminiQuotaExceeded):
                    quota_hits += 1
                    if quota_hits >= 3:
                        break
                # §3.16: bounded, jittered backoff between attempts — a
                # throttling provider must not receive a synchronized retry
                # wave from every worker (each new attempt keeps its own
                # fresh Phase 2 reservation via _metered_attempt).
                from backend.services.provider_backoff import provider_backoff
                await provider_backoff(e, quota_hits)
                continue
            raise
    from backend.services.llm_context import get_user_model
    if (model or get_user_model()) and last:
        raise last
    if last and is_quota_error(last):
        try:
            async with _metered_attempt(prompt, provider="groq",
                                        model=settings.GROQ_MODEL) as call:
                _t = await _groq_complete(prompt, json_mode=False, call=call)
            _record_process_local(call, prompt)
            return _t
        except Exception as groq_err:
            logger.warning("Groq text fallback failed (%s)", type(groq_err).__name__)
        try:
            async with _metered_attempt(prompt, provider="grok",
                                        model=settings.GROK_MODEL) as call:
                _t = await _grok_complete(prompt, json_mode=False, call=call)
            _record_process_local(call, prompt)
            return _t
        except Exception as grok_err:
            logger.warning("Grok text fallback failed: %s", grok_err)
        raise GeminiQuotaExceeded(
            "Gemini quota exhausted. Add a Gemini, Groq, or xAI key in Settings."
        ) from last
    try:
        async with _metered_attempt(prompt, provider="groq",
                                    model=settings.GROQ_MODEL) as call:
            _t = await _groq_complete(prompt, json_mode=False, call=call)
        _record_process_local(call, prompt)
        return _t
    except Exception as groq_err:
        logger.warning("Groq text fallback failed (%s)", type(groq_err).__name__)
    try:
        async with _metered_attempt(prompt, provider="grok",
                                    model=settings.GROK_MODEL) as call:
            return await _grok_complete(prompt, json_mode=False, call=call)
    except Exception:
        raise last or GeminiQuotaExceeded("Gemini quota exhausted. Add your own key in Settings.")


async def _generate_json_attempt(prompt: str, schema: Optional[Type] = None, model: Optional[str] = None) -> Any:
    """JSON generation. The model usage is settled from usage metadata BEFORE
    application parsing matters: an invalid-JSON response still consumed
    tokens, and each repair call is another metered attempt (§2.10)."""
    from backend.services.llm_context import active_llm_provider
    if active_llm_provider() in ("azure", "bedrock"):
        parsed = _coerce_json(await _adk_prompt(prompt))
        if not isinstance(parsed, (dict, list)):
            raise ValueError("The model did not return a JSON object or array")
        return parsed
    if _ollama_is_only_configured_provider():
        parsed = _coerce_json(await _ollama_json_complete(prompt, model=model))
        if not isinstance(parsed, (dict, list)):
            # One bounded repair attempt (§2.10: each repair is another
            # metered call) — small local models occasionally wrap the
            # plan in prose despite "Return JSON only".
            repair_prompt = (prompt + "\n\nIMPORTANT: Output ONLY a single "
                             "valid JSON object. No prose, no markdown, no "
                             "code fences — the response must start with '{' "
                             "and be parseable by json.loads.")
            parsed = _coerce_json(await _ollama_json_complete(
                repair_prompt, model=model))
        if not isinstance(parsed, (dict, list)):
            raise ValueError("The model did not return valid JSON")
        return parsed
    if _huggingface_is_only_configured_provider():
        async with _metered_attempt(prompt, provider="huggingface",
                                    model=model or settings.HF_MODEL, purpose="generation_json") as call:
            parsed = _coerce_json(await _huggingface_complete(prompt, json_mode=True, model=model, call=call))
        _record_process_local(call, prompt)
        return parsed
    if not model and _groq_is_only_configured_provider():
        async with _metered_attempt(prompt, provider="groq",
                                    model=settings.GROQ_MODEL, purpose="generation_json") as call:
            parsed = _coerce_json(await _groq_complete(prompt, json_mode=True, call=call))
        _record_process_local(call, prompt)
        return parsed
    from google.genai import types

    last: Optional[Exception] = None
    quota_hits = 0
    for candidate in _candidate_models(model):
        try:
            async with _metered_attempt(prompt, provider="gemini", model=candidate,
                                        purpose="generation_json") as call:
                client = _client()
                config_kwargs = {
                    "response_mime_type": "application/json",
                    "temperature": 0.1,
                    "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
                }
                if schema is not None:
                    config_kwargs["response_schema"] = schema
                await call.mark_dispatched()
                response = await _generate_content_async(
                    client,
                    model=candidate,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_kwargs),
                )
                call.capture_usage(_usage_metadata(response, candidate))
                text = (response.text or "").strip()
                call.capture_response_text(text)
                if not text:
                    raise ValueError("Model returned empty JSON")
                try:
                    parsed = _coerce_json(text)
                except json.JSONDecodeError:
                    logger.warning("Model JSON was invalid; requesting a repaired payload")
                    # Repair is a separate paid attempt with its own reservation.
                    fixed = await generate_text(
                        "Fix this into valid JSON only. Escape quotes and newlines inside strings. "
                        "No markdown fences.\n\n" + text[:24000],
                        model=candidate,
                    )
                    parsed = _coerce_json(fixed)
                if candidate != (model or settings.GEMINI_MODEL):
                    logger.info("Gemini JSON used fallback model %s", candidate)
                _record_process_local(call, prompt)
                return parsed
        except Exception as e:
            last = e
            if is_retryable_model_error(e):
                logger.warning("Gemini model %s unavailable (%s); trying another", candidate, e)
                if is_quota_error(e) or isinstance(e, GeminiQuotaExceeded):
                    quota_hits += 1
                    if quota_hits >= 3:
                        break
                from backend.services.provider_backoff import provider_backoff
                await provider_backoff(e, quota_hits)
                continue
            logger.warning("Gemini JSON failed on %s (%s); trying another", candidate, e)
            continue
    from backend.services.llm_context import get_user_model
    if (model or get_user_model()) and last:
        raise last
    if last and (is_quota_error(last) or isinstance(last, GeminiQuotaExceeded)):
        try:
            async with _metered_attempt(prompt, provider="groq",
                                        model=settings.GROQ_MODEL, purpose="generation_json") as call:
                _t = await _groq_complete(prompt, json_mode=True, call=call)
            _record_process_local(call, prompt)
            return _coerce_json(_t)
        except Exception as groq_err:
            logger.warning("Groq JSON fallback failed (%s)", type(groq_err).__name__)
        try:
            async with _metered_attempt(prompt, provider="grok",
                                        model=settings.GROK_MODEL, purpose="generation_json") as call:
                _t = await _grok_complete(prompt, json_mode=True, call=call)
            _record_process_local(call, prompt)
            return _coerce_json(_t)
        except Exception as grok_err:
            logger.warning("Grok JSON fallback failed: %s", grok_err)
        raise GeminiQuotaExceeded(
            "Gemini quota exhausted. Add a Groq or xAI key as a fallback."
        ) from last
    try:
        async with _metered_attempt(prompt, provider="groq",
                                    model=settings.GROQ_MODEL, purpose="generation_json") as call:
            parsed = _coerce_json(await _groq_complete(prompt, json_mode=True, call=call))
        _record_process_local(call, prompt)
        return parsed
    except Exception as groq_err:
        logger.warning("Groq JSON fallback failed (%s)", type(groq_err).__name__)
    try:
        async with _metered_attempt(prompt, provider="grok",
                                    model=settings.GROK_MODEL, purpose="generation_json") as call:
            return _coerce_json(await _grok_complete(prompt, json_mode=True, call=call))
    except Exception:
        raise last or ValueError("Model returned empty JSON")


async def describe_image(image_bytes: bytes, mime: str = "image/jpeg", name: str = "photo") -> str:
    """Describe an attached photo so the planner can use it. Multimodal input
    is metered with a conservative flat per-image estimate — the 4-chars/token
    heuristic does not apply to image bytes (§2.12)."""
    from google.genai import types

    part = None
    if hasattr(types.Part, "from_bytes"):
        part = types.Part.from_bytes(data=image_bytes, mime_type=mime or "image/jpeg")
    else:
        part = types.Part(inline_data=types.Blob(data=image_bytes, mime_type=mime or "image/jpeg"))
    contents = [
        part,
        (
            f"Describe this attached photo ({name}) for an automation agent. "
            "Extract visible text, UI labels, URLs, tables, and what the user likely wants done."
        ),
    ]
    IMAGE_ESTIMATE_UNITS = 1000  # conservative flat estimate for one image
    last: Optional[Exception] = None
    quota_hits = 0
    for candidate in _candidate_models():
        try:
            async with _metered_attempt("", provider="gemini", model=candidate,
                                        purpose="vision", extra_units=IMAGE_ESTIMATE_UNITS) as call:
                client = _client()
                await call.mark_dispatched()
                response = await _generate_content_async(
                    client,
                    model=candidate,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    ),
                )
                call.capture_usage(_usage_metadata(response, candidate))
                text = (response.text or f"A photo named {name} was attached.").strip()
                call.capture_response_text(text)
                _record_process_local(call, name)
                return text
        except Exception as e:
            last = e
            if is_retryable_model_error(e):
                logger.warning("Image describe Gemini %s unavailable (%s)", candidate, e)
                from backend.services.provider_backoff import provider_backoff
                await provider_backoff(e, quota_hits)
                continue
            break
    e = last
    if e and is_quota_error(e):
        try:
            async with _metered_attempt(
                f"Describe this attached photo ({name}) for an automation agent from the filename and mime type {mime}. "
                "The image bytes could not be sent to Gemini. Say that a photo was attached and what the user likely wants.",
                provider="grok", model=settings.GROK_MODEL, purpose="vision",
            ) as call:
                text = await _grok_complete(
                    f"Describe this attached photo ({name}) for an automation agent from the filename and mime type {mime}. "
                    "The image bytes could not be sent to Gemini. Say that a photo was attached and what the user likely wants.",
                    json_mode=False,
                    call=call,
                )
            _record_process_local(call, name)
            return text
        except Exception:
            raise GeminiQuotaExceeded(
                "Gemini quota exhausted. Add your own key in Settings."
            ) from e
    if e:
        logger.warning("Image describe failed: %s", e)
    return f"A photo named {name} was attached."


async def search_web(query: str) -> list[dict]:
    """Discover sources through Google Search grounding metadata. Grounded
    search requests are billable model calls — metered like the rest (§2.11)."""
    from google.genai import types
    async with _metered_attempt(query, provider="gemini",
                                model=candidate_models()[0], purpose="search") as call:
        client = _client()
        await call.mark_dispatched()
        response = await _generate_content_async(
            client, model=candidate_models()[0], contents=query,
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
        )
        call.capture_usage(_usage_metadata(response, candidate_models()[0]))
    sources = {}
    for candidate in response.candidates or []:
        grounding = getattr(candidate, "grounding_metadata", None)
        for chunk in getattr(grounding, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            uri = getattr(web, "uri", None)
            if uri and uri.startswith("https://"):
                sources[uri] = {"url": uri, "title": getattr(web, "title", "") or "", "snippet": ""}
    if not sources:
        raise RuntimeError("Search returned no grounded sources; research cannot be verified")
    return list(sources.values())


async def generate_text(prompt: str, model: Optional[str] = None) -> str:
    """Public entry: every paid provider attempt holds its own durable budget
    reservation (§2.11) — reserve estimated max → dispatch → settle actuals."""
    return await _generate_text_attempt(prompt, model=model)


async def generate_json(prompt: str, schema: Optional[Type] = None, model: Optional[str] = None) -> Any:
    return await _generate_json_attempt(prompt, schema=schema, model=model)
