"""Provider usage normalization (Phase 2, §2.12).

Converts provider-specific usage metadata into one common receipt while
retaining the provider-specific dimensions needed for reconciliation.

Rules enforced here:
- Missing provider fields stay UNKNOWN (None), never false zeroes.
- Cached / reasoning tokens are not double-counted when the provider already
  includes them in reported totals.
- The provider's reported total is kept alongside the normalized value.
- Text-length estimation is an ESTIMATE (measurement_source="estimated").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

NORMALIZER_VERSION = "usage-normalizer-1"

# ~4 characters per token is an estimate for planning, never a hard-cap proof.
CHARS_PER_TOKEN = 4


@dataclass
class NormalizedUsage:
    provider: str
    requested_model: Optional[str] = None
    resolved_model: Optional[str] = None
    provider_request_id: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    reported_total_tokens: Optional[int] = None
    measurement_source: str = "unknown"
    raw_metadata: Dict[str, Any] = field(default_factory=dict)
    normalizer_version: str = NORMALIZER_VERSION

    @property
    def known(self) -> bool:
        return (
            self.input_tokens is not None
            or self.output_tokens is not None
            or self.reported_total_tokens is not None
        )

    def policy_units(self) -> int:
        """Units counted against the budget policy. Cached input tokens are
        billed differently by providers; Phase 2 counts them (conservative)
        but never adds cached/reasoning ON TOP of a reported total that
        already contains them. When only the reported total exists, use it.
        """
        if self.reported_total_tokens is not None:
            return max(0, int(self.reported_total_tokens))
        total = 0
        if self.input_tokens is not None:
            total += int(self.input_tokens)
        if self.output_tokens is not None:
            total += int(self.output_tokens)
        return max(0, total)

    def to_receipt_kwargs(self) -> Dict[str, Any]:
        return {
            "provider_request_id": self.provider_request_id,
            "resolved_model": self.resolved_model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "reported_total_tokens": self.reported_total_tokens,
            "raw_metadata": self.raw_metadata,
        }


def normalize_gemini_usage(usage_metadata: Any, *, requested_model: Optional[str] = None,
                           resolved_model: Optional[str] = None) -> NormalizedUsage:
    """google-genai GenerateContentResponseUsageMetadata (also used by ADK)."""
    def _get(name: str) -> Optional[int]:
        value = getattr(usage_metadata, name, None)
        if value is None and isinstance(usage_metadata, dict):
            value = usage_metadata.get(name)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    prompt = _get("prompt_token_count")
    candidates = _get("candidates_token_count")
    cached = _get("cached_content_token_count")
    thoughts = _get("thoughts_token_count")
    total = _get("total_token_count")
    raw: Dict[str, Any] = {}
    for name in ("prompt_token_count", "candidates_token_count",
                 "cached_content_token_count", "thoughts_token_count",
                 "total_token_count"):
        value = _get(name)
        if value is not None:
            raw[name] = value
    return NormalizedUsage(
        provider="gemini",
        requested_model=requested_model,
        resolved_model=resolved_model,
        input_tokens=prompt,
        output_tokens=candidates,
        cached_input_tokens=cached,
        reasoning_tokens=thoughts,
        reported_total_tokens=total,
        measurement_source="provider_reported" if (prompt is not None or total is not None) else "unknown",
        raw_metadata=raw,
    )


def _detail_value(details: Any, key: str) -> Optional[int]:
    """Read a nested detail field from a dict OR an object (OpenAI SDKs
    return ``prompt_tokens_details`` as a pydantic object in some client
    versions and a plain dict in others)."""
    if details is None:
        return None
    if isinstance(details, dict):
        value = details.get(key)
    else:
        value = getattr(details, key, None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _preserve_details(raw: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Keep nested provider detail objects inspectable (30.32). The flat
    metadata filter drops dicts/objects; nested usage details carry the
    cache/reasoning inclusion rules reconcilers need, so they are stored
    as JSON strings alongside the flat fields."""
    for name in ("prompt_tokens_details", "completion_tokens_details",
                 "cached_tokens", "reasoning_tokens"):
        value = usage.get(name)
        if value is None or isinstance(value, (int, float, str)):
            continue
        try:
            import json as _json
            raw[name] = _json.dumps(value, default=str)
        except Exception:
            raw[name] = str(value)


def normalize_openai_usage(usage: Any, *, provider: str,
                           requested_model: Optional[str] = None,
                           resolved_model: Optional[str] = None) -> NormalizedUsage:
    """OpenAI-compatible chat usage (Groq, Grok/xAI, Hugging Face router).

    `usage` may be a dict from response.json() {"usage": {...}} or the usage
    object itself. completion_tokens_details.reasoning_tokens and
    prompt_tokens_details.cached_tokens are honored when present but NOT
    added twice (providers include both subsets in the reported totals —
    a receipt of 1,000 input including 300 cached plus 200 output
    including 40 reasoning settles 1,200 units, with 300/40 exposed as
    subsets)."""
    if usage is None:
        return NormalizedUsage(provider=provider, requested_model=requested_model,
                               resolved_model=resolved_model,
                               measurement_source="unknown")
    if not isinstance(usage, dict):
        obj = usage
        usage = {
            "prompt_tokens": getattr(obj, "prompt_tokens", None),
            "completion_tokens": getattr(obj, "completion_tokens", None),
            "total_tokens": getattr(obj, "total_tokens", None),
            "completion_tokens_details": getattr(obj, "completion_tokens_details", None),
            "prompt_tokens_details": getattr(obj, "prompt_tokens_details", None),
        }
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    reasoning = _detail_value(usage.get("completion_tokens_details"), "reasoning_tokens")
    cached = _detail_value(usage.get("prompt_tokens_details"), "cached_tokens")
    if cached is None and isinstance(usage.get("cached_tokens"), int):
        # Some providers expose cached tokens at the top level.
        cached = int(usage["cached_tokens"])

    def _int(value) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    raw: Dict[str, Any] = {k: v for k, v in usage.items()
                           if isinstance(v, (int, float, str))}
    _preserve_details(raw, usage)
    return NormalizedUsage(
        provider=provider,
        requested_model=requested_model,
        resolved_model=resolved_model,
        input_tokens=_int(prompt),
        output_tokens=_int(completion),
        cached_input_tokens=cached,
        reasoning_tokens=_int(reasoning),
        reported_total_tokens=_int(total),
        measurement_source="provider_reported" if (prompt is not None or total is not None) else "unknown",
        raw_metadata=raw,
    )


def estimate_units_from_text(*texts: Any) -> int:
    """Estimate the units a request/response occupies (~4 chars/token).
    ALWAYS paired with measurement_source="estimated" by callers."""
    total = 0
    for text in texts:
        if not text:
            continue
        if not isinstance(text, str):
            try:
                import json as _json
                text = _json.dumps(text, default=str)
            except Exception:
                text = str(text)
        total += (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN
    return total
