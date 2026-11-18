"""
AgentOS — JSON safety property tests

Regression tests for the audit's #1 (HIGH) bug: responses emitted
RFC-8259-invalid JSON when AI/tool output contained HTML, quotes, or
newlines. Every payload below must round-trip through SafeJSONResponse
to bytes that Python's strict json.loads — and therefore any browser —
can parse, and that encode as clean UTF-8.
"""

import json

import pytest

from backend.security.json_safety import (
    SafeJSONResponse,
    safe_json_dumps,
    sanitize_json_string,
    sanitize_json_value,
)

NASTY_STRINGS = [
    "<html><body><h1>Site Report</h1></body></html>",
    'He said "quote me" and \\ backslashes \\ everywhere',
    "line1\nline2\r\nline3\ttabbed",
    "unicode: café 中文 \U0001f680 emoji",
    "control\x00\x01\x02chars\x1f",
    "js-separators \u2028 \u2029 inside",
    "</script><script>alert(1)</script>",
    '{"already": "json-looking"}',
    "mixed \\\" escaped \\n and real \n newline",
    "trailing backslash at end \\",
    "",  # empty
    "a" * 100_000,  # large blob
]

LONE_SURROGATES = [
    "lone low surrogate: \udcff",
    "lone high surrogate: \ud83d",
    "pair then lone: \U0001f680\udcff",
    "\ud800\ud800 two highs",
]


@pytest.mark.parametrize("payload", NASTY_STRINGS)
def test_response_is_valid_json_for_hostile_strings(payload):
    body = {"result": payload, "nested": {"html": payload}}
    response = SafeJSONResponse(body)
    raw = response.body if isinstance(response.body, bytes) else bytes(response.body)
    # Must encode as clean UTF-8 (no lone-surrogate crash/broken bytes)
    text = raw.decode("utf-8")
    # Strict parse — exactly what a browser's JSON.parse does
    parsed = json.loads(text)
    assert parsed["nested"]["html"] == sanitize_json_string(payload)


@pytest.mark.parametrize("payload", LONE_SURROGATES)
def test_lone_surrogates_become_replacement_chars(payload):
    cleaned = sanitize_json_string(payload)
    # Clean UTF-8 round-trip with no exceptions
    cleaned.encode("utf-8").decode("utf-8")
    assert "\udcff" not in cleaned and "\ud83d" not in cleaned


def test_workflow_run_payload_with_html_output():
    """The exact audit scenario: /workflows/{id} where a tool returned HTML."""
    run = {
        "run_id": "run-123",
        "workflow_id": "wf-123",
        "user_id": "u1",
        "goal": "Check https://example.com and report the HTML",
        "status": "COMPLETED",
        "tasks": [
            {
                "task_id": "t1",
                "agent": "core.http",
                "status": "COMPLETED",
                "input_data": {"url": "https://example.com"},
                "output_data": {
                    "status_code": 200,
                    "data": "<!DOCTYPE html>\n<html lang=\"en\">\n<head><meta charset=\"utf-8\">",
                },
            }
        ],
    }
    response = SafeJSONResponse(run)
    parsed = json.loads(bytes(response.body).decode("utf-8"))
    assert parsed["tasks"][0]["output_data"]["data"].startswith("<!DOCTYPE html>")


def test_sse_frames_stay_parseable():
    event = {"summary": "Task done", "metadata": {"output": "<b>bold</b>\nnew\r\nline"}}
    frame = f"id: evt-1\ndata: {safe_json_dumps(event)}\n\n"
    lines = frame.split("\n")
    data_line = next(l for l in lines if l.startswith("data: "))
    # Data line must be single-line (SSE framing) and strictly parseable
    parsed = json.loads(data_line[len("data: "):])
    assert parsed["metadata"]["output"] == "<b>bold</b>\nnew\r\nline"


def test_deep_recursion_and_mixed_types():
    value = {"a": [1, 2.5, None, True, {"b": ("<x>", ["y\n"])}], "ts": "2026-10-16T00:00:00Z"}
    response = SafeJSONResponse(value)
    parsed = json.loads(bytes(response.body).decode("utf-8"))
    assert parsed["a"][4]["b"][0] == "<x>"
    assert parsed["a"][4]["b"][1][0] == "y\n"


def test_sanitize_value_preserves_non_strings():
    assert sanitize_json_value(42) == 42
    assert sanitize_json_value(None) is None
    assert sanitize_json_value(True) is True
    assert sanitize_json_value(3.14) == 3.14
    assert sanitize_json_value([1, "x\x00"]) == [1, "x"]
    assert sanitize_json_value({"k\x00": "v\x00"}) == {"k": "v"}


def test_nan_and_inf_rejected_not_emitted():
    with pytest.raises(ValueError):
        SafeJSONResponse({"x": float("nan")})
