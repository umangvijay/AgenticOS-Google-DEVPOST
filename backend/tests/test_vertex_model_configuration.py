"""Model selection and request compatibility, with no provider requests."""
from types import SimpleNamespace

import pytest
from google.genai import types

from backend.config.settings import Settings
from backend.services import gemini_client


@pytest.mark.parametrize("model", [
    "gemini-4-hydrogen", "gemini-4-argon", "gemini-3.1-pro-preview",
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash",
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
])
def test_pinned_vertex_models_are_selectable_without_substitution(monkeypatch, model):
    monkeypatch.setattr(gemini_client.settings, "GEMINI_ALLOWED_MODELS",
                        Settings.model_fields["GEMINI_ALLOWED_MODELS"].default)
    assert gemini_client._candidate_models(model) == [model]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini-4-hydrogen", "gemini-4-argon"])
async def test_gemini4_keeps_schema_and_signed_parts_without_sampling(model):
    captured = {}
    def generate(**kwargs):
        captured.update(kwargs)
        return "response"
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    config = types.GenerateContentConfig(
        temperature=0.2, top_p=0.9, top_k=20,
        response_mime_type="application/json",
        response_schema={"type": "OBJECT", "properties": {"ok": {"type": "BOOLEAN"}}},
    )
    contents = [types.Content(role="model", parts=[
        types.Part(text="prior answer", thought_signature=b"signature")])]
    assert await gemini_client._generate_content_async(
        client, model=model, contents=contents, config=config) == "response"
    sent = captured["config"]
    assert sent.temperature is None and sent.top_p is None and sent.top_k is None
    assert sent.response_mime_type == "application/json"
    assert sent.response_schema == config.response_schema
    assert captured["contents"] is contents
    assert contents[0].parts[0].thought_signature == b"signature"
    assert config.temperature == 0.2  # caller-owned configuration is unchanged


@pytest.mark.asyncio
async def test_flash_request_keeps_its_sampling_configuration():
    captured = {}
    client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kwargs: captured.update(kwargs)))
    config = types.GenerateContentConfig(temperature=0.2)
    await gemini_client._generate_content_async(
        client, model="gemini-3.8-flash", contents="hello", config=config)
    assert captured["config"] is config
    assert config.temperature == 0.2
