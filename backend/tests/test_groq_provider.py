"""Groq provider fallback tests; all HTTP responses are stubbed."""
import pytest

from backend.config.settings import settings
from backend.services import gemini_client, llm_context


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    response = _Response()
    request = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, *, headers, json):
        type(self).request = (url, headers, json)
        return type(self).response


@pytest.fixture(autouse=True)
def groq_only_provider(monkeypatch):
    llm_context.set_user_groq_key("test-groq-key")
    llm_context.set_user_gemini_key(None)
    llm_context.set_user_grok_key(None)
    monkeypatch.setattr(settings, "GOOGLE_CLOUD_PROJECT", None)
    monkeypatch.setattr(settings, "GROQ_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setattr("httpx.AsyncClient", _Client)
    yield
    llm_context.set_user_groq_key(None)


@pytest.mark.asyncio
async def test_generate_text_uses_groq_when_it_is_the_only_provider(budget_repo):
    _Client.response = _Response(200, {"choices": [{"message": {"content": "real answer"}}]})
    answer = await gemini_client.generate_text("Say hello")
    assert answer == "real answer"
    url, headers, payload = _Client.request
    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer test-groq-key"
    assert payload["model"] == "openai/gpt-oss-20b"


@pytest.mark.asyncio
async def test_generate_json_uses_groq_json_mode(budget_repo):
    _Client.response = _Response(200, {"choices": [{"message": {"content": '{"ready": true}'}}]})
    result = await gemini_client.generate_json("Return JSON with ready true")
    assert result == {"ready": True}
    assert _Client.request[2]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_groq_error_does_not_echo_provider_body_or_credential():
    _Client.response = _Response(403, text="test-groq-key must not be leaked")
    with pytest.raises(RuntimeError) as raised:
        await gemini_client._groq_complete("test")
    assert str(raised.value) == "Groq HTTP 403"
    assert "test-groq-key" not in str(raised.value)
