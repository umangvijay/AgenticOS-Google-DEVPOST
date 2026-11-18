"""Phase 2 provider-coverage tests (§2.11).

Acceptance condition: every supported provider dispatch can be associated
with a durable reservation — shared generation (text/JSON + repair), model
fallbacks, ADK adapters, embeddings, and direct SDK services. Also proves
fail-closed: with the reservation store unavailable, the provider is never
called (G7).
"""
import asyncio
from types import SimpleNamespace

import pytest

from backend.config.settings import settings
from backend.models.usage import (
    BudgetError,
    BudgetErrorCode,
    BudgetScope,
    ReservationState,
)
from backend.repositories.usage_repository import set_usage_repo
from backend.services import gemini_client, llm_context


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fake_response(text: str, prompt_tokens: int = 100, output_tokens: int = 50):
    return SimpleNamespace(
        text=text,
        model_version="gemini-test",
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=output_tokens,
            total_token_count=prompt_tokens + output_tokens,
        ),
    )


@pytest.fixture
def gemini_provider(monkeypatch):
    """Stub the Gemini SDK and select the gemini provider."""
    llm_context.set_user_id("coverage-user")
    llm_context.set_user_gemini_key("test-key")
    llm_context.set_user_groq_key(None)
    llm_context.set_user_grok_key(None)
    llm_context.set_user_hf_token(None)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")
    sent = []

    async def fake_generate(client, **kwargs):
        sent.append(kwargs)
        return _fake_response('{"ok": true}')

    monkeypatch.setattr(gemini_client, "_client", lambda: None)
    monkeypatch.setattr(gemini_client, "_generate_content_async", fake_generate)
    return sent


# ── shared generation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_text_reservation_settles_from_usage_metadata(gemini_provider, budget_repo):
    answer = await gemini_client.generate_text("hello")
    assert answer == '{"ok": true}'
    snapshot = await budget_repo.get_budget_snapshot(
        BudgetScope.USER, "coverage-user", _today(), 10_000_000
    )
    assert snapshot.consumed == 150, "usage metadata (100 in + 50 out) must be settled"
    assert snapshot.reserved == 0


@pytest.mark.asyncio
async def test_invalid_json_repair_creates_a_second_metered_attempt(gemini_provider, budget_repo):
    """A provider may return invalid JSON: the model usage is still settled,
    and the repair call is another paid attempt with its own reservation."""
    calls = {"n": 0}

    async def flaky_generate(client, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_response("this is not json {")  # forces repair
        return _fake_response('{"fixed": true}')

    import backend.services.gemini_client as gc
    import unittest.mock as mock
    with mock.patch.object(gc, "_generate_content_async", flaky_generate):
        result = await gc.generate_json("give me json")
    assert result == {"fixed": True}
    snapshot = await budget_repo.get_budget_snapshot(
        BudgetScope.USER, "coverage-user", _today(), 10_000_000
    )
    # First attempt (150) + repair attempt (150) = both metered.
    assert snapshot.consumed == 300
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_quota_rejection_releases_the_reservation(gemini_provider, budget_repo):
    """429 rejections never generated: hold released, not parked uncertain."""
    import backend.services.gemini_client as gc

    async def quota_generate(client, **kwargs):
        raise RuntimeError("429 RESOURCE_EXHAUSTED quota exhausted")

    import unittest.mock as mock
    with mock.patch.object(gc, "_generate_content_async", quota_generate), \
         mock.patch.object(gc, "_candidate_models", lambda preferred=None: ["gemini-test"]):
        with pytest.raises(Exception):
            await gc.generate_text("hello")
    snapshot = await budget_repo.get_budget_snapshot(
        BudgetScope.USER, "coverage-user", _today(), 10_000_000
    )
    assert snapshot.reserved == 0, "quota rejection must not strand a hold"
    assert snapshot.consumed == 0


@pytest.mark.asyncio
async def test_timeout_after_dispatch_stays_uncertain(gemini_provider, budget_repo):
    import backend.services.gemini_client as gc

    async def hanging_generate(client, **kwargs):
        raise TimeoutError("provider timed out after transmission")

    import unittest.mock as mock
    with mock.patch.object(gc, "_generate_content_async", hanging_generate), \
         mock.patch.object(gc, "_candidate_models", lambda preferred=None: ["gemini-test"]):
        with pytest.raises(TimeoutError):
            await gc.generate_text("hello")
    snapshot = await budget_repo.get_budget_snapshot(
        BudgetScope.USER, "coverage-user", _today(), 10_000_000
    )
    assert snapshot.reserved > 0, "timeout must not release the allowance silently"
    assert snapshot.uncertain_reserved == snapshot.reserved


@pytest.mark.asyncio
async def test_reservation_store_unavailable_blocks_the_provider_call(gemini_provider, monkeypatch):
    """G7: missing accounting → the provider is never called."""
    import backend.services.gemini_client as gc

    async def fail_generate(client, **kwargs):
        raise AssertionError("provider must not be called when accounting is unavailable")

    set_usage_repo(None)
    try:
        import unittest.mock as mock
        with mock.patch.object(gc, "_generate_content_async", fail_generate):
            with pytest.raises(BudgetError) as exc:
                await gc.generate_text("hello")
        assert exc.value.code == BudgetErrorCode.ACCOUNTING_UNAVAILABLE
    finally:
        pass


# ── ADK adapters ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adk_groq_invocation_is_metered(monkeypatch, budget_repo):
    from backend.services.groq_adk import GroqADK
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    class _Response:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": "planner says hi"}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
            }

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, *, headers, json):
            return _Response()

    llm_context.set_user_id("adk-user")
    llm_context.set_user_groq_key("k")
    monkeypatch.setattr("httpx.AsyncClient", _Client)
    request = LlmRequest(
        model="openai/gpt-oss-20b",
        contents=[types.Content(role="user", parts=[types.Part(text="plan this")])],
    )
    responses = [r async for r in GroqADK(model="openai/gpt-oss-20b").generate_content_async(request)]
    assert responses[0].content.parts[0].text == "planner says hi"
    snapshot = await budget_repo.get_budget_snapshot(
        BudgetScope.USER, "adk-user", _today(), 10_000_000
    )
    assert snapshot.consumed == 50, "ADK invocation must be settled from provider usage"
    assert snapshot.reserved == 0


# ── embeddings ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_embedding_request_is_metered(monkeypatch, budget_repo):
    from backend.services.embedding_service import GoogleCloudEmbeddingService, embed_text_metered

    llm_context.set_user_id("embed-user")

    def fake_embed(self, text):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(GoogleCloudEmbeddingService, "embed_text", fake_embed)
    vectors = await embed_text_metered(GoogleCloudEmbeddingService(), "remember this memory")
    assert vectors == [0.1, 0.2, 0.3]
    snapshot = await budget_repo.get_budget_snapshot(
        BudgetScope.USER, "embed-user", _today(), 10_000_000
    )
    assert snapshot.consumed > 0, "embedding usage must be attributed"
    assert snapshot.reserved == 0


# ── direct SDK services ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jd_parser_and_resume_tailor_are_metered(gemini_provider, budget_repo):
    from backend.services.jd_parser_service import JDParserService
    from backend.services.resume_tailor_service import ResumeTailorService
    from backend.models.resume import Resume

    jd_payload = {
        "title": "Backend Engineer", "company": "Acme",
        "required_skills": ["python"], "preferred_skills": [],
        "responsibilities": [], "educational_requirements": [],
    }
    resume_payload = {
        "id": "r1",
        "contact": {"name": "Test User", "email": "test@example.com"},
        "summary": "engineer", "skills": ["python"],
        "experience": [], "education": [], "projects": [],
    }

    async def generate_by_prompt(client, **kwargs):
        prompt = str(kwargs.get("contents"))
        if "Extract the structured details" in prompt:
            return _fake_response('{"title":"Backend Engineer","company":"Acme",'
                                  '"required_skills":["python"],"preferred_skills":[],'
                                  '"responsibilities":[],"educational_requirements":[],'
                                  '"raw_text":"python role"}')
        return _fake_response('{"id":"r1",'
                              '"contact":{"name":"Test User","email":"test@example.com"},'
                              '"summary":"engineer","skills":["python"],'
                              '"experience":[],"education":[],"projects":[]}')

    import backend.services.gemini_client as gc
    import unittest.mock as mock
    with mock.patch.object(gc, "_generate_content_async", generate_by_prompt):
        jd = await JDParserService().parse_job_description("python role")
        master = Resume.model_validate(resume_payload)
        await ResumeTailorService().tailor(master, jd, "job-1")

    snapshot = await budget_repo.get_budget_snapshot(
        BudgetScope.USER, "coverage-user", _today(), 10_000_000
    )
    assert snapshot.consumed == 300, "both direct-SDK services must be metered (2 × 150)"


# ── web agent (goes through generate_json) ────────────────────────────


@pytest.mark.asyncio
async def test_web_agent_decision_call_is_metered(gemini_provider, budget_repo):
    import json as _json
    import backend.services.gemini_client as gc
    import unittest.mock as mock

    async def decision_generate(client, **kwargs):
        return _fake_response(_json.dumps({"action": "done", "success": True, "result": "ok"}))

    with mock.patch.object(gc, "_generate_content_async", decision_generate):
        decision = await gc.generate_json("decide browser action")
    assert decision["action"] == "done"
    snapshot = await budget_repo.get_budget_snapshot(
        BudgetScope.USER, "coverage-user", _today(), 10_000_000
    )
    assert snapshot.consumed == 150
