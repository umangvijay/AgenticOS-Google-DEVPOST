"""
AgentOS — Canonical JSON Response Layer

One escaping/sanitizing layer for every JSON response. Guarantees:

1. Output is RFC-8259 valid JSON, always.
2. Output encodes to clean UTF-8: lone surrogates (common in scraped HTML,
   PDF-extracted text, and LLM output) are replaced before serialization —
   with ensure_ascii=False, a lone surrogate makes Starlette's
   content.encode("utf-8") fail or emit broken bytes, which strict clients
   experience as "invalid JSON".
3. Control characters (U+0000–U+001F) never appear raw in the output.

Every router response and every manually constructed JSONResponse goes
through SafeJSONResponse, so the sanitization lives in exactly one place.
"""

import json
import re
from typing import Any

from starlette.responses import JSONResponse as StarletteJSONResponse

# Control characters are escaped by json.dumps anyway, but strip them from the
# in-memory values so downstream consumers (DB round-trips, SSE frames, logs)
# never carry raw control bytes from untrusted text.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_json_string(value: str) -> str:
    """Replace lone surrogates and strip control characters from a string."""
    if not value:
        return value
    if _CONTROL_CHARS.search(value):
        value = _CONTROL_CHARS.sub("", value)
    # encode(strict) raises on lone surrogates; those become U+FFFD
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")


def sanitize_json_value(value: Any) -> Any:
    """Recursively sanitize strings inside any JSON-serializable structure."""
    if isinstance(value, str):
        return sanitize_json_string(value)
    if isinstance(value, dict):
        return {
            sanitize_json_string(str(k)) if isinstance(k, str) else k: sanitize_json_value(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_json_value(item) for item in value]
    return value


class SafeJSONResponse(StarletteJSONResponse):
    """JSONResponse that guarantees parseable, clean UTF-8 output."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            sanitize_json_value(content),
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


def safe_json_dumps(content: Any, **kwargs: Any) -> str:
    """Sanitize + serialize to a JSON string (for SSE frames and templates)."""
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("default", str)
    return json.dumps(sanitize_json_value(content), **kwargs)
