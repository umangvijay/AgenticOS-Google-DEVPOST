"""
AgentOS — Input sanitizer regression tests

Covers the audit's memory-write 500: untrusted text carrying lone UTF-16
surrogates crashed SQLite writes (UnicodeEncodeError) and corrupted JSON
responses. sanitize_text must repair them before storage.
"""

import pytest

from backend.security.input_sanitizer import sanitize_text, InputValidationError


def test_lone_surrogates_are_repaired_not_rejected():
    dirty = "report \udcff tail and \ud83d high"
    cleaned = sanitize_text(dirty, "content")
    cleaned.encode("utf-8")  # must not raise
    assert "\udcff" not in cleaned and "\ud83d" not in cleaned
    assert "report" in cleaned


def test_valid_unicode_passes_through_unchanged():
    text = "café 中文 \U0001f680 — fine"
    assert sanitize_text(text, "content") == text


def test_control_bytes_still_blocked_by_traversal_rules():
    # Null bytes are a path-traversal pattern and must still be rejected.
    with pytest.raises(InputValidationError):
        sanitize_text("bad\x00null", "content")


def test_xss_still_blocked():
    with pytest.raises(InputValidationError):
        sanitize_text("<script>alert(1)</script>", "content")
