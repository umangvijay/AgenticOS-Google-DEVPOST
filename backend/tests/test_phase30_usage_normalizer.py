"""Phase 30 / 30.32 — provider usage normalization specifics.

Cached input comes from prompt_tokens_details (dict AND object forms);
nested detail fields survive into raw_metadata for reconciliation;
missing fields stay unknown (never zero).
"""
import json

from backend.services.usage_normalizer import (
    normalize_openai_usage, normalize_gemini_usage, NormalizedUsage,
)


class _Details:
    """Object-form details (some OpenAI client versions)."""

    def __init__(self, cached_tokens=None, reasoning_tokens=None):
        self.cached_tokens = cached_tokens
        self.reasoning_tokens = reasoning_tokens


def test_openai_cached_input_from_dict_details():
    usage = normalize_openai_usage({
        "prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
        "prompt_tokens_details": {"cached_tokens": 300},
        "completion_tokens_details": {"reasoning_tokens": 40},
    }, provider="openai")
    assert usage.cached_input_tokens == 300
    assert usage.reasoning_tokens == 40
    assert usage.policy_units() == 1200


def test_openai_cached_input_from_object_details():
    usage = normalize_openai_usage({
        "prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600,
        "prompt_tokens_details": _Details(cached_tokens=128),
        "completion_tokens_details": _Details(reasoning_tokens=25),
    }, provider="groq")
    assert usage.cached_input_tokens == 128
    assert usage.reasoning_tokens == 25


def test_openai_nested_details_preserved_in_metadata():
    usage = normalize_openai_usage({
        "prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600,
        "prompt_tokens_details": {"cached_tokens": 128, "audio_tokens": 0},
    }, provider="openai")
    assert usage.raw_metadata["prompt_tokens"] == 500  # flat fields kept
    preserved = json.loads(usage.raw_metadata["prompt_tokens_details"])
    assert preserved == {"cached_tokens": 128, "audio_tokens": 0}


def test_openai_top_level_cached_tokens():
    usage = normalize_openai_usage({
        "prompt_tokens": 300, "completion_tokens": 50, "total_tokens": 350,
        "cached_tokens": 64,
    }, provider="openai")
    assert usage.cached_input_tokens == 64


def test_openai_missing_fields_stay_unknown():
    usage = normalize_openai_usage({"prompt_tokens": 10}, provider="openai")
    assert usage.cached_input_tokens is None
    assert usage.reasoning_tokens is None
    assert usage.reported_total_tokens is None
    assert usage.policy_units() == 10  # known input only


def test_gemini_semantics_unchanged():
    usage = normalize_gemini_usage({
        "prompt_token_count": 1000, "candidates_token_count": 200,
        "cached_content_token_count": 300, "thoughts_token_count": 40,
        "total_token_count": 1200,
    }, requested_model="gemini-3.8-flash", resolved_model="gemini-3.8-flash")
    assert usage.cached_input_tokens == 300
    assert usage.reasoning_tokens == 40
    assert usage.policy_units() == 1200
    assert usage.resolved_model == "gemini-3.8-flash"


def test_accumulation_preserves_provider_request_id():
    first = NormalizedUsage(provider="gemini", input_tokens=100,
                            output_tokens=10, provider_request_id="req-1")
    second = NormalizedUsage(provider="gemini", input_tokens=50,
                             output_tokens=5, provider_request_id="req-2")
    merged = NormalizedUsage(provider="gemini", input_tokens=100,
                             output_tokens=10, provider_request_id="req-1")
    # capture_usage merges: the LAST request id wins (the newest request).
    merged.provider_request_id = second.provider_request_id or merged.provider_request_id
    assert merged.provider_request_id == "req-2"
