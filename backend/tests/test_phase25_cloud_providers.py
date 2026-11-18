"""Cloud adapter boundary tests; these are not hosted qualification evidence."""
import asyncio

import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from backend.config.settings import settings
from backend.services import llm_context
from backend.services.azure_adk import AzureADK, azure_endpoint


@pytest.mark.parametrize("url", ["http://demo.openai.azure.com", "https://127.0.0.1",
    "https://demo.openai.azure.com.evil.test", "https://user@demo.openai.azure.com",
    "https://demo.openai.azure.com/?api-key=bad", "https://demo.openai.azure.com:444"])
def test_azure_rejects_untrusted_endpoint(monkeypatch, url):
    monkeypatch.setattr(settings, "AZURE_OPENAI_ENDPOINT", url)
    with pytest.raises(ValueError):
        azure_endpoint()


@pytest.mark.asyncio
async def test_cloud_credentials_and_provider_are_isolated_between_tasks(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
    async def actor(name, provider):
        token = llm_context._provider_override.set(provider)
        bedrock = llm_context._user_bedrock_bearer.set(name)
        azure = llm_context._user_azure_key.set(name)
        try:
            await asyncio.sleep(0.01)
            return (llm_context.active_llm_provider(), llm_context.get_user_bedrock_bearer(),
                    llm_context.effective_azure_key())
        finally:
            llm_context._provider_override.reset(token)
            llm_context._user_bedrock_bearer.reset(bedrock)
            llm_context._user_azure_key.reset(azure)
    assert await asyncio.gather(actor("alice", "azure"), actor("bob", "bedrock")) == [
        ("azure", "alice", "alice"), ("bedrock", "bob", "bob")]
    assert settings.LLM_PROVIDER == "ollama"


@pytest.mark.asyncio
async def test_azure_maps_tools_and_records_usage(monkeypatch, budget_repo):
    from backend.tests.test_groq_adk import _Client, _Response
    class Response(_Response):
        @staticmethod
        def json():
            data = _Response.json()
            data["usage"] = {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23}
            return data
    class Client(_Client):
        async def post(self, url, *, headers, json):
            await super().post(url, headers=headers, json=json)
            return Response()
    monkeypatch.setattr("httpx.AsyncClient", Client)
    monkeypatch.setattr(settings, "AZURE_OPENAI_ENDPOINT", "https://agentos-test.openai.azure.com")
    monkeypatch.setattr(settings, "AZURE_OPENAI_DEPLOYMENT", "agentos-chat")
    provider_token = llm_context._provider_override.set("azure")
    key_token = llm_context._user_azure_key.set("synthetic-test-key")
    user_token = llm_context._user_id.set("azure-test-user")
    run_token = llm_context._run_id.set("azure-test-run")
    try:
        model = llm_context.adk_gemini()
        assert isinstance(model, AzureADK)
        request = LlmRequest(model=model.model, contents=[types.Content(role="user", parts=[types.Part(text="Fetch")])],
            config=types.GenerateContentConfig(tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name="fetch", parameters_json_schema={"type": "object"})])]))
        replies = [r async for r in model.generate_content_async(request)]
        assert replies[0].content.parts[0].function_call.name == "fetch"
        assert Client.seen["url"] == "https://agentos-test.openai.azure.com/openai/v1/chat/completions"
        assert Client.seen["headers"] == {"api-key": "synthetic-test-key"}
        assert Client.seen["json"]["model"] == "agentos-chat"
        assert "max_completion_tokens" in Client.seen["json"]
        assert "max_tokens" not in Client.seen["json"]
        summary = await budget_repo.usage_summary_for_run("azure-test-run")
        assert summary["providers"] == ["azure"]
        assert summary["total_units"] == 23
    finally:
        llm_context._provider_override.reset(provider_token)
        llm_context._user_azure_key.reset(key_token)
        llm_context._user_id.reset(user_token)
        llm_context._run_id.reset(run_token)


@pytest.mark.asyncio
async def test_bedrock_sigv4_signs_the_transmitted_bytes(monkeypatch):
    import hashlib
    import botocore.auth
    import botocore.credentials
    import botocore.session
    from backend.services.bedrock_adk import BedrockADK
    signed = {}
    original_signer = botocore.auth.SigV4Auth
    class Signer(original_signer):
        def add_auth(self, request):
            signed["body"] = request.body
            super().add_auth(request)
    class Session:
        def get_credentials(self):
            return botocore.credentials.Credentials("synthetic-access", "synthetic-secret", "synthetic-session")
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, *, content, headers):
            assert content == signed["body"]
            assert hashlib.sha256(content).digest() == hashlib.sha256(signed["body"]).digest()
            assert "Authorization" in headers
            assert headers["X-Amz-Security-Token"] == "synthetic-session"
            return "sent"
    monkeypatch.setattr(botocore.session, "get_session", Session)
    monkeypatch.setattr(botocore.auth, "SigV4Auth", Signer)
    monkeypatch.setattr("httpx.AsyncClient", Client)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    token = llm_context._provider_override.set("bedrock")
    try:
        adapter = llm_context.adk_gemini()
        assert isinstance(adapter, BedrockADK)
        assert await adapter._post_signed({"messages": [{"text": "hello café"}]}, None) == "sent"
    finally:
        llm_context._provider_override.reset(token)
