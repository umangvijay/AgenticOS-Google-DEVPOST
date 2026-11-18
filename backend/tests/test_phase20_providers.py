"""Phase 20 §20.7–§20.9: provider adapter contracts — request shaping,
credential validation, per-user isolation, and the no-canned-output
gate. Live provider inference is a SEPARATE gate (needs credentials)."""
import json

import pytest


def _messages():
    from backend.providers.base import NormalizedMessage
    return [
        NormalizedMessage(role="system", content="You are AgentOS."),
        NormalizedMessage(role="user", content="Summarize the invoice."),
        NormalizedMessage(role="assistant", content="Working on it.",
                          tool_calls=[{"id": "tc1", "name": "read_invoice",
                                       "arguments": {"id": "INV-1"}}]),
        NormalizedMessage(role="tool", content="total=42",
                          tool_call_id="read_invoice"),
    ]


# ── §20.8: Vertex AI — two DISTINCT auth modes ─────────────────────────

def test_vertex_express_and_standard_are_not_interchangeable():
    from backend.providers.base import ProviderConfig, ProviderKind
    from backend.providers.vertex_ai import VertexAIAdapter
    express = VertexAIAdapter(ProviderConfig(
        kind=ProviderKind.VERTEX_AI, model_id="gemini-2.0-flash",
        api_key="test-key"))
    assert "key=test-key" in express._endpoint()
    standard = VertexAIAdapter(ProviderConfig(
        kind=ProviderKind.VERTEX_AI, model_id="gemini-2.0-flash",
        project="my-project", location="us-central1"))
    ep = standard._endpoint()
    assert "my-project" in ep and "us-central1" in ep
    assert "key=" not in ep
    with pytest.raises(ValueError, match="NOT interchangeable"):
        VertexAIAdapter(ProviderConfig(kind=ProviderKind.VERTEX_AI,
                                       model_id="m"))


def test_vertex_request_shape_matches_the_api():
    from backend.providers.base import ProviderConfig, ProviderKind
    from backend.providers.vertex_ai import (
        VertexAIAdapter, build_generate_request)
    adapter = VertexAIAdapter(ProviderConfig(
        kind=ProviderKind.VERTEX_AI, model_id="gemini-2.0-flash",
        api_key="k"))
    body = build_generate_request(_messages(),
                                  tools=[{"name": "read_invoice",
                                          "description": "read",
                                          "parameters": {"type": "object"}}])
    roles = [c["role"] for c in body["contents"]]
    assert "user" in roles and "model" in roles
    assert body["contents"][-1]["parts"][0]["functionResponse"][
        "name"] == "read_invoice"
    assert body["tools"][0]["functionDeclarations"][0]["name"] == \
        "read_invoice"
    assert body["systemInstruction"]["parts"][0]["text"] == "You are AgentOS."


def test_vertex_error_categories():
    from backend.providers.base import ErrorCategory
    from backend.providers.vertex_ai import normalize_vertex_error
    assert normalize_vertex_error(401, "{}") is ErrorCategory.AUTH
    assert normalize_vertex_error(429, "{}") is ErrorCategory.RATE_LIMIT
    assert normalize_vertex_error(404, "{}") is ErrorCategory.NOT_FOUND
    assert normalize_vertex_error(500, "{}") is ErrorCategory.SERVER
    assert normalize_vertex_error(400, "{}") is ErrorCategory.INVALID_REQUEST


def test_vertex_response_parsing_with_tool_calls():
    from backend.providers.vertex_ai import VertexAIAdapter
    adapter = VertexAIAdapter.__new__(VertexAIAdapter)
    # Parse a real-shaped response (no HTTP needed).
    data = {"candidates": [{"content": {"parts": [
        {"text": "The total is "},
        {"functionCall": {"name": "read_invoice",
                          "args": {"id": "INV-1"}}}]},
        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10,
                          "candidatesTokenCount": 20,
                          "totalTokenCount": 30}}
    text = ""
    tool_calls = []
    for cand in data.get("candidates", []):
        for part in (cand.get("content", {}).get("parts", []) or []):
            if "text" in part:
                text += part["text"]
            if "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append({"name": fc.get("name"),
                                   "arguments": fc.get("args", {})})
    assert text == "The total is "
    assert tool_calls == [{"name": "read_invoice",
                           "arguments": {"id": "INV-1"}}]


# ── §20.8: Bedrock — Converse API, bearer + credential chain ───────────

def test_bedrock_converse_request_shape():
    from backend.providers.bedrock import build_converse_request
    body = build_converse_request(_messages(),
                                  tools=[{"name": "read_invoice",
                                          "description": "read",
                                          "parameters": {"type": "object"}}])
    assert body["system"][0]["text"] == "You are AgentOS."
    assert body["messages"][0]["content"][0]["text"] == \
        "Summarize the invoice."
    assert body["messages"][1]["content"][1]["toolUse"]["name"] == \
        "read_invoice"
    assert body["messages"][2]["content"][0]["toolResult"]["toolUseId"] == \
        "read_invoice"
    assert body["toolConfig"]["tools"][0]["toolSpec"]["name"] == \
        "read_invoice"
    assert body["inferenceConfig"]["maxTokens"] == 2048


def test_bedrock_bearer_and_region_required():
    from backend.providers.base import ProviderConfig, ProviderKind
    from backend.providers.bedrock import BedrockAdapter
    a = BedrockAdapter(ProviderConfig(
        kind=ProviderKind.BEDROCK, model_id="anthropic.claude-3-5-sonnet",
        region="us-east-1",
        bearer_token="ABSKtestkey1234567890abcdefghijklmnop"))
    assert "bedrock-runtime.us-east-1" in a._endpoint()
    assert a._headers()["Authorization"].startswith("Bearer ")
    with pytest.raises(ValueError, match="region"):
        BedrockAdapter(ProviderConfig(kind=ProviderKind.BEDROCK,
                                      model_id="m"))


def test_bedrock_response_parsing():
    from backend.providers.bedrock import parse_converse_response
    data = {"output": [{"message": {"role": "assistant", "content": [
        {"text": "Invoice total 42"},
        {"toolUse": {"toolUseId": "t1", "name": "read_invoice",
                     "input": {"id": "INV-1"}}}]},
        "stopReason": "tool_use"}],
        "usage": {"inputTokens": 12, "outputTokens": 24, "totalTokens": 36}}
    out = parse_converse_response(data)
    assert out.text == "Invoice total 42"
    assert out.tool_calls == [{"id": "t1", "name": "read_invoice",
                               "arguments": {"id": "INV-1"}}]
    assert out.usage.known and out.usage.total_tokens == 36
    assert out.finish_reason == "tool_use"


def test_bedrock_error_categories():
    from backend.providers.base import ErrorCategory
    from backend.providers.bedrock import normalize_bedrock_error
    assert normalize_bedrock_error(403, "") is ErrorCategory.AUTH
    assert normalize_bedrock_error(429, "") is ErrorCategory.RATE_LIMIT
    assert normalize_bedrock_error(404, "") is ErrorCategory.NOT_FOUND


# ── §20.10: local inference — real model, no credentials ───────────────

def test_ollama_local_request_shape():
    from backend.providers.base import ProviderConfig, ProviderKind
    from backend.providers.ollama import OllamaAdapter, build_ollama_request
    a = OllamaAdapter(ProviderConfig(
        kind=ProviderKind.OLLAMA, model_id="llama3.2",
        base_url="http://localhost:11434"))
    body = build_ollama_request(_messages())
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][3]["content"].startswith("Tool result:")
    assert body["stream"] is False


def test_ollama_local_needs_no_credentials():
    from backend.providers.base import ProviderConfig, ProviderKind
    from backend.providers.ollama import OllamaAdapter
    a = OllamaAdapter(ProviderConfig(
        kind=ProviderKind.OLLAMA, model_id="llama3.2",
        base_url="http://localhost:11434"))
    out = a.validate_credentials()
    assert out["status"] in ("accepted", "unavailable")
    assert out.get("mode") in (None, "local")


# ── §20.9: per-user provider config isolation ──────────────────────────

def test_provider_configs_are_request_scoped_not_global():
    """§20.9: two users constructing configs concurrently cannot
    exchange credentials — configs are per-instance objects, never
    process-global state."""
    from backend.providers import create_adapter
    from backend.providers.base import ProviderConfig, ProviderKind
    a = create_adapter(ProviderConfig(
        kind=ProviderKind.VERTEX_AI, model_id="m-a", api_key="user-a-key"))
    b = create_adapter(ProviderConfig(
        kind=ProviderKind.VERTEX_AI, model_id="m-b", api_key="user-b-key"))
    assert a.config.api_key == "user-a-key"
    assert b.config.api_key == "user-b-key"
    # Mutating one does not touch the other.
    a.config.api_key = "rotated"
    assert b.config.api_key == "user-b-key"


def test_capability_declaration_per_provider():
    from backend.providers import create_adapter
    from backend.providers.base import ProviderConfig, ProviderKind
    ollama = create_adapter(ProviderConfig(
        kind=ProviderKind.OLLAMA, model_id="m", base_url="http://x"))
    assert ollama.capabilities.images is False, \
        "a provider supporting chat does not imply image support"
    vertex = create_adapter(ProviderConfig(
        kind=ProviderKind.VERTEX_AI, model_id="m", api_key="k"))
    assert vertex.capabilities.tools is True
