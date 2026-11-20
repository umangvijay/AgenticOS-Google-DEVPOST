from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.config.settings import Settings
from backend.services import gemini_client, llm_context
from backend.services.gemini_catalog import catalog_payload, workflow_eligible
from backend.providers.base import ProviderConfig, ProviderKind
from backend.providers.vertex_ai import VertexAIAdapter
from backend.api.routers.settings_router import SettingsUpdate, update_settings


@pytest.mark.parametrize("model", ["gemini-4-neon", "gemini-4-carbon",
    "gemini-3.1-pro-preview-customtools", "gemini-3-flash-preview"])
def test_new_models_keep_exact_user_selection(monkeypatch, model):
    monkeypatch.setattr(gemini_client.settings, "LLM_PROVIDER", "vertex")
    monkeypatch.setattr(gemini_client.settings, "GEMINI_ALLOWED_MODELS", Settings.model_fields["GEMINI_ALLOWED_MODELS"].default)
    assert gemini_client.candidate_models(model) == [model]


def test_catalog_does_not_confuse_enabled_with_verified():
    result = catalog_payload(["gemini-4-argon", "gemini-unknown"], provider="vertex", default_model="gemini-4-argon")
    rows = {r["id"]: r for r in result["models"]}
    assert rows["gemini-4-argon"]["selectable"] is True
    assert rows["gemini-4-argon"]["access"] == "unverified"
    assert rows["gemini-4-neon"]["selectable"] is False
    assert rows["gemini-unknown"]["selectable"] is False
    assert result["auth_mode"] == "service_account_adc"


def test_retirement_boundary_and_specialized_models():
    assert workflow_eligible("gemini-3.6-flash", date(2026, 11, 18))
    assert not workflow_eligible("gemini-3.6-flash", date(2026, 11, 19))
    assert not workflow_eligible("gemini-embedding-001")
    assert not workflow_eligible("gemini-omni-1.2-flash")


@pytest.mark.asyncio
async def test_disabled_model_rejected_by_settings_and_execution(monkeypatch):
    monkeypatch.setattr(gemini_client.settings, "LLM_PROVIDER", "vertex")
    monkeypatch.setattr(gemini_client.settings, "GEMINI_ALLOWED_MODELS", "gemini-3.8-flash,gemini-embedding-001,gemini-made-up")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(factory=object())))
    user = SimpleNamespace(user_id="test-user")
    for model in ["gemini-4-argon", "gemini-embedding-001", "gemini-made-up"]:
        with pytest.raises(HTTPException) as error:
            await update_settings(SettingsUpdate(default_model=model), request, user)
        assert error.value.status_code == 400
        with pytest.raises(ValueError):
            await llm_context.load_user_model(None, "", model)


def test_other_provider_not_given_gemini_catalog():
    result = catalog_payload(["private-deployment"], provider="azure", default_model="private-deployment")
    assert [m["id"] for m in result["models"]] == ["private-deployment"]
    assert result["models"][0]["selectable"]


@pytest.mark.asyncio
async def test_saved_choice_survives_worker_reload_and_adk_dispatch(monkeypatch):
    monkeypatch.setattr(gemini_client.settings, "LLM_PROVIDER", "vertex")
    monkeypatch.setattr(gemini_client.settings, "GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setattr(gemini_client.settings, "GEMINI_ALLOWED_MODELS", "gemini-4-carbon,gemini-4-neon")
    class Repo:
        async def get_settings(self, user_id):
            return {"default_model": "gemini-4-carbon"}
    token = llm_context._user_model.set(None)
    try:
        assert await llm_context.load_user_model(Repo(), "user") == "gemini-4-carbon"
        assert llm_context.adk_gemini().model == "gemini-4-carbon"
        # A task's stored explicit choice wins if the user's default changes.
        assert await llm_context.load_user_model(Repo(), "user", "gemini-4-neon") == "gemini-4-neon"
        assert llm_context.adk_gemini().model == "gemini-4-neon"
    finally:
        llm_context._user_model.reset(token)


def test_global_vertex_uses_global_hostname():
    adapter = VertexAIAdapter(ProviderConfig(kind=ProviderKind.VERTEX_AI,
        model_id="gemini-4-argon", project="test-project", location="global"))
    assert adapter._endpoint() == "https://aiplatform.googleapis.com/v1/projects/test-project/locations/global/publishers/google/models/gemini-4-argon:generateContent"


@pytest.mark.asyncio
async def test_express_stream_preserves_key_query(monkeypatch):
    captured = {}
    class Response:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            yield 'data: {"candidates":[{"content":{"parts":[{"text":"OK"}]}}]}'
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, method, endpoint, **kwargs):
            captured["endpoint"] = endpoint
            return Response()
    monkeypatch.setattr("httpx.AsyncClient", Client)
    adapter = VertexAIAdapter(ProviderConfig(kind=ProviderKind.VERTEX_AI,
        model_id="gemini-3.8-flash", api_key="synthetic-key"))
    results = [r async for r in adapter.generate_stream([])]
    assert results[0].text == "OK"
    assert captured["endpoint"].endswith(":streamGenerateContent?key=synthetic-key&alt=sse")
