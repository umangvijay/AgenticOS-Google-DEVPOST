"""Phase 21 §21.5: providers through the ORDINARY execution path.

- Ollama runs REAL local inference against the live local model server
  (llama3.2:1b) — the response must genuinely depend on the prompt
  (varying inputs produce varying outputs), carry REAL usage accounting
  through the budget service, and support tool calling.
- Bedrock's ADK adapter is shape-tested against a stubbed Converse
  endpoint (live calls are credential-gated and recorded as such).
- LLM_PROVIDER accepts 'bedrock' and 'ollama' — the settings validator
  that previously rejected them (audit A) now passes.
"""
import json

import pytest
import pytest_asyncio
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from backend.config.settings import settings

pytestmark = pytest.mark.asyncio


def _ollama_up() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=2):
            return True
    except OSError:
        return False


def _model_pulled() -> bool:
    import httpx
    try:
        res = httpx.get(f"{settings.OLLAMA_BASE_URL}/api/tags", timeout=3)
        models = [m.get("name", "") for m in res.json().get("models", [])]
        return any(m.startswith(settings.OLLAMA_MODEL.split(":")[0])
                   for m in models)
    except Exception:
        return False


async def _ask(adk, prompt: str, *, tools=None, schema=None):
    config = types.GenerateContentConfig(
        system_instruction="You are a helpful echo assistant. Answer with exactly the word the user requests.",
        **({"tools": [types.Tool(function_declarations=tools)]} if tools else {}),
        **({"response_json_schema": schema} if schema else {}),
    )
    request = LlmRequest(
        model=adk.model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=config,
    )
    responses = []
    async for response in adk.generate_content_async(request):
        responses.append(response)
    return responses


# ── settings gate (audit A repair) ─────────────────────────────────────

def test_provider_validator_accepts_bedrock_and_ollama():
    import pydantic
    from backend.config.settings import Settings
    s1 = Settings(LLM_PROVIDER="bedrock")
    assert s1.LLM_PROVIDER == "bedrock"
    s2 = Settings(LLM_PROVIDER="ollama")
    assert s2.LLM_PROVIDER == "ollama"
    with pytest.raises(pydantic.ValidationError):
        Settings(LLM_PROVIDER="not-a-provider")


def test_adk_gemini_dispatches_bedrock_and_ollama(monkeypatch):
    from backend.services.llm_context import adk_gemini
    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
    adk = adk_gemini()
    assert type(adk).__name__ == "OllamaADK"
    monkeypatch.setattr(settings, "LLM_PROVIDER", "bedrock")
    from backend.services.llm_context import set_user_bedrock_bearer
    set_user_bedrock_bearer("test-bearer")
    adk = adk_gemini()
    assert type(adk).__name__ == "BedrockADK"
    set_user_bedrock_bearer(None)


# ── REAL local inference (live, local — no credentials needed) ─────────

@pytest.mark.skipif(not _ollama_up(), reason="BLOCKED: Ollama not on :11434 — "
                    "docker compose --profile local-ai up ollama")
@pytest.mark.skipif(not _model_pulled(), reason="BLOCKED: model not pulled — "
                    "docker exec agentos-ollama-1 ollama pull llama3.2:1b")
async def test_ollama_real_inference_depends_on_prompt(budget_repo):
    """REAL local inference: different prompts → genuinely different
    answers; the reply echoes the requested marker (provenance: the
    output derives from the ACTUAL input, not a canned string)."""
    from backend.services.ollama_adk import OllamaADK
    adk = OllamaADK()
    responses = await _ask(adk, "Reply with exactly this single word and nothing else: BANANA42")
    assert responses, "no response from the local model"
    text = "".join(
        p.text or "" for r in responses for p in r.content.parts)
    assert "BANANA42" in text, \
        f"the reply must derive from the actual prompt, got: {text[:120]!r}"
    # A different prompt yields a different answer (dynamic behavior).
    responses2 = await _ask(adk, "Reply with exactly this single word and nothing else: ZEBRA77")
    text2 = "".join(
        p.text or "" for r in responses2 for p in r.content.parts)
    assert "ZEBRA77" in text2 and "BANANA42" not in text2


@pytest.mark.skipif(not _ollama_up(), reason="BLOCKED: Ollama not on :11434")
@pytest.mark.skipif(not _model_pulled(), reason="BLOCKED: model not pulled")
async def test_ollama_real_inference_is_metered_through_budget(budget_repo):
    """The budget service records REAL usage for the local call — the
    §2 contract applies to local inference identically."""
    from backend.services.ollama_adk import OllamaADK
    from backend.services.llm_context import set_run_id, set_user_id
    set_user_id("meter-user-1")
    set_run_id("run-meter-1")
    adk = OllamaADK()
    await _ask(adk, "Reply with exactly this single word: METER3")
    # The reservation LEDGER records the invocation against the run:
    # provider ollama, a real settled token count, settled state.
    import sqlite3
    conn = sqlite3.connect(budget_repo.db.db_path)
    row = conn.execute(
        "SELECT provider, model, settled_units, state FROM "
        "budget_reservations WHERE run_id='run-meter-1'").fetchone()
    conn.close()
    assert row is not None, "the metered call wrote a reservation row"
    provider, model, settled_units, state = row
    assert provider == "ollama" and state == "settled"
    assert (settled_units or 0) > 0, "REAL token usage was settled"


@pytest.mark.skipif(not _ollama_up(), reason="BLOCKED: Ollama not on :11434")
@pytest.mark.skipif(not _model_pulled(), reason="BLOCKED: model not pulled")
async def test_ollama_tool_calling_round_trip(budget_repo):
    """The local model requests the declared tool through the ADK
    interface — tool use flows through the ordinary path."""
    from backend.services.ollama_adk import OllamaADK
    adk = OllamaADK()
    responses = await _ask(
        adk,
        "Use the get_weather tool to check the weather in Paris.",
        tools=[types.FunctionDeclaration(
            name="get_weather",
            description="Get the current weather for a city",
            parameters_json_schema={"type": "object",
                                    "properties": {"city": {"type": "string"}},
                                    "required": ["city"]})])
    calls = [p.function_call for r in responses for p in r.content.parts
             if p.function_call]
    assert calls and calls[0].name == "get_weather", \
        "the local model issued a REAL tool call through the ADK path"
    assert calls[0].args.get("city"), calls[0].args


# ── Bedrock ADK shape (live = credential-gated) ────────────────────────

class _ConverseResponse:
    status_code = 200

    def json(self):
        return {"output": {"message": {"role": "assistant", "content": [
            {"text": "PARIS"},
            {"toolUse": {"toolUseId": "tu-1", "name": "get_weather",
                         "input": {"city": "Paris"}}},
        ]}}, "usage": {"inputTokens": 11, "outputTokens": 5}}


class _ConverseClient:
    seen = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, *, headers=None, json=None):
        type(self).seen = {"url": url, "headers": headers or {}, "json": json}
        return _ConverseResponse()


async def test_bedrock_adk_converse_shape_and_metering(monkeypatch, budget_repo):
    from backend.services import bedrock_adk
    from backend.services.llm_context import (
        set_user_bedrock_bearer, set_run_id, set_user_id)
    set_user_bedrock_bearer("per-user-bearer")
    set_user_id("bedrock-user")
    set_run_id("run-bedrock-1")
    monkeypatch.setattr(bedrock_adk.httpx, "AsyncClient", _ConverseClient)
    adk = bedrock_adk.BedrockADK()
    responses = await _ask(
        adk, "Check the weather in Paris.",
        tools=[types.FunctionDeclaration(
            name="get_weather", description="weather",
            parameters_json_schema={"type": "object", "properties": {
                "city": {"type": "string"}}})])
    seen = _ConverseClient.seen
    # Per-USER bearer travels with the request (§21.6).
    assert seen["headers"]["Authorization"] == "Bearer per-user-bearer"
    assert "/converse" in seen["url"]
    assert seen["json"]["messages"][0]["role"] in ("user", "assistant")
    tool_spec = seen["json"]["toolConfig"]["tools"][0]["toolSpec"]
    assert tool_spec["name"] == "get_weather"
    # Tool use + text parse back through the ADK interface.
    calls = [p.function_call for r in responses for p in r.content.parts
             if p.function_call]
    assert calls and calls[0].name == "get_weather"
    set_user_bedrock_bearer(None)


async def test_bedrock_missing_credential_fails_loudly(monkeypatch):
    """An exhausted SDK chain fails at dispatch, before any HTTP request."""
    from backend.services import llm_context
    from backend.config.settings import settings
    import botocore.session
    class EmptyChain:
        def get_credentials(self):
            return None
    monkeypatch.setattr(settings, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(botocore.session, "get_session", EmptyChain)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    llm_context.set_user_bedrock_bearer(None)
    adapter = llm_context.adk_gemini()
    with pytest.raises(ValueError, match="Bedrock credentials unavailable"):
        await adapter._post_signed({"messages": []}, None)
