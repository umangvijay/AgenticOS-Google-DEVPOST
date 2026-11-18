"""Phase 20 §20.10: the no-canned-output gate — production execution
consumes real authorized input and produces outputs from the configured
inference provider or real tool execution. Deterministic engineering
(schemas, validation, state machines) is allowed; fabricated model
output, fake progress, and silent demo fallbacks are not."""
import ast
import pathlib
import pytest

from backend.tests.test_phase26_web_agent_safety import runtime  # noqa: F401

BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_no_hardcoded_demo_responses_in_the_engine():
    """The engine's response path must come from model events — a
    hardcoded reply string constant would be a canned output."""
    src = (BACKEND / "engine" / "engine.py").read_text()
    # The engine builds the prompt from run.goal + task input (real
    # input), and extracts events from the model runner.
    assert "async for event in runner.run_async(" in src
    assert "types.UserContent(parts=[types.Part(text=prompt)])" in src
    # The prompt includes the REAL user goal, not a fixed string.
    assert "run.goal" in src and "task_input" in src


def test_missing_inference_is_a_visible_failure_not_fabrication():
    """A model error must FAIL the attempt (falling through to the next
    candidate or raising) — never produce a fixed success result."""
    src = (BACKEND / "engine" / "engine.py").read_text()
    # The loop records the failure and continues/raises; there is no
    # "return fixed success on exception" path in the model loop.
    assert "is_retryable_model_error" in src
    # The loop breaks only on success (rewarded_model set).
    assert "rewarded_model = model_name" in src


def test_no_hardcoded_account_or_resource_ids():
    """§20.10: no hardcoded account IDs, resource IDs, or credentials in
    the execution path — identifiers come from the request context."""
    forbidden = ["ACCT-12345", "hardcoded-account", "demo-account-id",
                 "example.com/api/fake"]
    for f in ("engine/engine.py", "services/artifact_builder.py",
              "services/web_agent.py"):
        src = (BACKEND / f).read_text()
        for token in forbidden:
            assert token not in src, f"{f} contains {token}"


@pytest.mark.asyncio
async def test_browser_actions_require_real_dispatch_evidence(runtime):
    """§20.10: simulated browser actions presented as executed are
    prohibited — executed steps carry dispatch evidence and errors are
    recorded, not hidden."""
    from unittest.mock import AsyncMock
    from backend.services.web_agent import WebAgent
    from backend.tests.test_phase26_web_agent_safety import register
    _, _, snapshot, page = runtime
    register(runtime)
    agent = WebAgent()
    agent._snapshot = AsyncMock(return_value=snapshot)
    agent._decide = AsyncMock(side_effect=[
        {"action": "click", "index": 0},
        {"action": "done", "result": "claimed success", "success": True},
    ])
    agent._execute = AsyncMock(side_effect=RuntimeError("transport failed"))
    result = await agent.run("Open details", page.url, "user", run_id="run", task_id="task")
    assert agent._execute.await_count == 1
    assert result["steps"][0]["executed"] is True
    assert result["steps"][0]["error"] == "transport failed"
    assert result["success"] is False


def test_providers_never_return_fixed_text_on_error():
    """Provider adapters map errors to categories — they never return a
    canned text response on failure."""
    for name in ("vertex_ai.py", "bedrock.py", "ollama.py"):
        src = (BACKEND / "providers" / name).read_text()
        # Error paths return empty text + an error category.
        assert "error_category=" in src
        # No hardcoded successful responses.
        assert '"The answer is' not in src
        assert '"Hello! I am' not in src


def test_deterministic_engineering_is_allowed_and_used():
    """§20.10: schemas, authorization, calculations, and state machines
    are legitimate deterministic code — they are not canned output."""
    from backend.services.connector_eligibility import (
        evaluate_capability_readiness)
    from backend.models.usage import utc_period_day
    # A deterministic authorization decision works without a model.
    out = evaluate_capability_readiness(
        None, {"build_id": "b"})
    assert out["ready"] is False   # deterministic, no inference needed
