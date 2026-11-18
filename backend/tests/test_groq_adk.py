"""Groq ADK adapter request/response mapping tests with a stubbed HTTP client."""
import pytest
from google.genai import types
from google.adk.models.llm_request import LlmRequest

from backend.config.settings import settings
from backend.services import gemini_client, llm_context
from backend.services.groq_adk import GroqADK
from backend.agents.orchestrator.orchestrator_agent import existing_integration_tools


class _Response:
    status_code = 200
    headers = {}

    @staticmethod
    def json():
        return {"choices": [{"message": {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "fetch", "arguments": '{"path":"/json"}'},
            }],
        }}]}


class _Client:
    seen = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, *, headers, json):
        type(self).seen = {"url": url, "headers": headers, "json": json}
        return _Response()


@pytest.mark.asyncio
async def test_groq_adk_maps_adk_tool_declarations_and_function_calls(monkeypatch, budget_repo):
    llm_context.set_user_groq_key("adapter-test-key")
    monkeypatch.setattr("httpx.AsyncClient", _Client)
    config = types.GenerateContentConfig(
        system_instruction="Use the provided tools.",
        tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
            name="fetch", description="Fetch an API path.",
            parameters_json_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )])],
    )
    request = LlmRequest(
        model="openai/gpt-oss-20b",
        contents=[types.Content(role="user", parts=[types.Part(text="Fetch /json")])],
        config=config,
    )
    try:
        responses = [r async for r in GroqADK(model="openai/gpt-oss-20b").generate_content_async(request)]
        assert len(responses) == 1
        function_call = responses[0].content.parts[0].function_call
        assert function_call.name == "fetch"
        assert function_call.args == {"path": "/json"}
        assert _Client.seen["headers"]["Authorization"] == "Bearer adapter-test-key"
        assert _Client.seen["json"]["tools"][0]["function"]["parameters"]["type"] == "object"
        assert _Client.seen["json"]["reasoning_effort"] == "low"
        assert _Client.seen["json"]["messages"][0] == {"role": "system", "content": "Use the provided tools."}
    finally:
        llm_context.set_user_groq_key(None)


@pytest.mark.asyncio
async def test_groq_adk_uses_json_schema_for_planner_output(monkeypatch, budget_repo):
    llm_context.set_user_groq_key("adapter-test-key")
    monkeypatch.setattr("httpx.AsyncClient", _Client)
    request = LlmRequest(
        model="openai/gpt-oss-20b",
        contents=[types.Content(role="user", parts=[types.Part(text="Plan this")])],
        config=types.GenerateContentConfig(response_schema={
            "type": "object", "properties": {"tasks": {"type": "array"}}, "required": ["tasks"],
        }),
    )
    try:
        _ = [r async for r in GroqADK(model="openai/gpt-oss-20b").generate_content_async(request)]
        response_format = _Client.seen["json"]["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["schema"]["properties"]["tasks"]["type"] == "array"
        assert response_format["json_schema"]["strict"] is False
    finally:
        llm_context.set_user_groq_key(None)


def test_explicit_groq_provider_takes_precedence_and_does_not_cycle_gemini_models(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "groq")
    monkeypatch.setattr(settings, "GROQ_MODEL", "openai/gpt-oss-20b")
    llm_context.set_user_groq_key("adapter-test-key")
    llm_context.set_user_gemini_key("gemini-key-present-but-not-selected")
    try:
        assert llm_context.active_llm_provider() == "groq"
        assert gemini_client.candidate_models() == ["openai/gpt-oss-20b"]
        assert isinstance(llm_context.adk_gemini(), GroqADK)
    finally:
        llm_context.set_user_groq_key(None)
        llm_context.set_user_gemini_key(None)


def test_orchestrator_reuses_matching_registered_integration():
    catalog = [
        {"mcp_name": "HTTPBin read-only", "agent_tool_name": "httpbin__getJson"},
        {"mcp_name": "Other API", "agent_tool_name": "other__list"},
    ]
    matches = existing_integration_tools(catalog, "HTTPBin API", "https://httpbin.org/openapi.json")
    assert [tool["agent_tool_name"] for tool in matches] == ["httpbin__getJson"]
    assert existing_integration_tools(catalog, "Different service", "https://different.example/openapi.json") == []
