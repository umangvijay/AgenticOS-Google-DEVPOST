"""Hugging Face Inference Providers adapter tests; network calls are stubbed."""

import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from backend.config.settings import settings
from backend.services import gemini_client, llm_context
from backend.services.huggingface_adk import HuggingFaceADK


class _Response:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Client:
    response = _Response({"choices": [{"message": {"content": "ok"}}]})
    seen = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, *, headers, json):
        type(self).seen = {"url": url, "headers": headers, "json": json}
        return type(self).response


@pytest.fixture(autouse=True)
def hf_provider(monkeypatch):
    llm_context.set_user_hf_token("hf-test-token")
    llm_context.set_user_gemini_key(None)
    llm_context.set_user_groq_key(None)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "huggingface")
    monkeypatch.setattr(settings, "HF_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setattr("httpx.AsyncClient", _Client)
    yield
    llm_context.set_user_hf_token(None)


@pytest.mark.asyncio
async def test_shared_text_and_json_use_huggingface_router(budget_repo):
    _Client.response = _Response({"choices": [{"message": {"content": "real answer"}}]})
    assert await gemini_client.generate_text("Say hello") == "real answer"
    url, headers, payload = _Client.seen["url"], _Client.seen["headers"], _Client.seen["json"]
    assert url == "https://router.huggingface.co/v1/chat/completions"
    assert headers["Authorization"] == "Bearer hf-test-token"
    assert payload["model"] == "openai/gpt-oss-120b"

    _Client.response = _Response({"choices": [{"message": {"content": '{"ready":true}'}}]})
    assert await gemini_client.generate_json("Return ready true") == {"ready": True}
    assert _Client.seen["json"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_hf_adk_maps_tool_calls_and_workflow_schema(monkeypatch, budget_repo):
    _Client.response = _Response({"choices": [{"message": {
        "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "fetch", "arguments": '{"path":"/json"}',
        }}],
    }}]})
    request = LlmRequest(
        model="openai/gpt-oss-120b",
        contents=[types.Content(role="user", parts=[types.Part(text="Fetch /json")])],
        config=types.GenerateContentConfig(
            system_instruction="Use only the declared tools.",
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
                name="fetch", description="Fetch a path.",
                parameters_json_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            )])],
        ),
    )
    result = [item async for item in HuggingFaceADK(model="openai/gpt-oss-120b").generate_content_async(request)]
    assert result[0].content.parts[0].function_call.args == {"path": "/json"}
    assert _Client.seen["json"]["tools"][0]["function"]["name"] == "fetch"

    _Client.response = _Response({"choices": [{"message": {"content": '{"tasks":[]}'}}]})
    schema_request = LlmRequest(
        model="openai/gpt-oss-120b",
        contents=[types.Content(role="user", parts=[types.Part(text="Plan")])],
        config=types.GenerateContentConfig(response_schema={
        "type": "object", "properties": {"tasks": {"type": "array"}}, "required": ["tasks"],
        }),
    )
    [item async for item in HuggingFaceADK(model="openai/gpt-oss-120b").generate_content_async(schema_request)]
    assert _Client.seen["json"]["response_format"]["type"] == "json_schema"


def test_provider_resolution_and_model_selection(monkeypatch):
    llm_context.set_user_hf_token("hf-test-token")
    llm_context.set_user_gemini_key("gemini-present-but-not-selected")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "huggingface")
    try:
        assert llm_context.active_llm_provider() == "huggingface"
        assert gemini_client.candidate_models() == ["openai/gpt-oss-120b"]
        assert gemini_client.allowed_models() == ["openai/gpt-oss-120b"]
        assert isinstance(llm_context.adk_gemini(), HuggingFaceADK)
    finally:
        llm_context.set_user_hf_token(None)
        llm_context.set_user_gemini_key(None)


@pytest.mark.asyncio
async def test_provider_error_does_not_expose_token(monkeypatch, budget_repo):
    # Exercise direct failure sanitization without trusting provider error text.
    class Forbidden:
        status_code = 403
        headers = {}
        def json(self):
            return {"error": "hf-test-token reflected by upstream"}
    class ForbiddenClient(_Client):
        async def post(self, url, *, headers, json):
            return Forbidden()
    monkeypatch.setattr("httpx.AsyncClient", ForbiddenClient)
    with pytest.raises(RuntimeError, match="HTTP 403") as raised:
        await gemini_client.generate_text("hello")
    assert "hf-test-token" not in str(raised.value)
    assert "Inference Providers access" in str(raised.value)

    class PaymentRequired:
        status_code = 402
        headers = {}
        def json(self):
            return {"error": "account credits exhausted"}
    class PaymentClient(_Client):
        async def post(self, url, *, headers, json):
            return PaymentRequired()
    monkeypatch.setattr("httpx.AsyncClient", PaymentClient)
    with pytest.raises(RuntimeError, match="credits or billing"):
        await gemini_client.generate_text("hello")
