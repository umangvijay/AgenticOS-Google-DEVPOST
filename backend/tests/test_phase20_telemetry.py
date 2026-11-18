"""Phase 20 §20.25: telemetry correlation — a journey is traceable via
ids. Spans carry run/task/agent/invocation/effect/browser/artifact/
recovery/policy ids as ATTRIBUTES; nested spans INHERIT them through
context; logs inject them via a filter; sensitive values never land on
telemetry. Verified with a REAL in-memory span exporter (no mocks of
the correlation logic)."""
import logging
import uuid

import pytest

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter)


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    from backend.instrumentation import telemetry as tel
    processor = SimpleSpanProcessor(exporter)
    tel.trace_provider.add_span_processor(processor)
    yield exporter
    try:
        tel.trace_provider._active_span_processor.remove_span_processor(
            processor)
    except Exception:  # pragma: no cover — best-effort teardown
        pass


def test_span_records_journey_ids_as_attributes(spans):
    from backend.instrumentation.telemetry import correlated_span
    ids = dict(run_id="run-x1", task_id="task-y2", agent_id="agent-z3",
               invocation_id="inv-1", effect_id="fx-1",
               browser_session_id="bs-1", artifact_id="a-1",
               recovery_episode_id="ep-1", policy_version="p5")
    with correlated_span("journey.op", **ids):
        pass
    span = spans.get_finished_spans()[-1]
    for k, v in ids.items():
        assert span.attributes[k] == v, f"{k} missing on span attributes"


def test_nested_spans_inherit_correlation_ids(spans):
    """A child operation started INSIDE the journey context carries the
    same ids — the journey stays traceable across modules."""
    from backend.instrumentation.telemetry import (
        correlated_span, current_correlation)
    with correlated_span("run.dispatch", run_id="run-x1",
                         task_id="task-y2"):
        assert current_correlation()["run_id"] == "run-x1"
        with correlated_span("model.invoke", invocation_id="inv-9",
                             agent_id="agent-z3"):
            corr = current_correlation()
            assert corr["run_id"] == "run-x1", "parent id inherited"
            assert corr["invocation_id"] == "inv-9"
            assert "policy_version" not in corr, \
                "an unset id stays unset in the inherited context"
        with correlated_span("effect.execute", effect_id="fx-7"):
            assert current_correlation()["effect_id"] == "fx-7"
            assert current_correlation()["run_id"] == "run-x1"
    assert "run_id" not in current_correlation(), \
        "ids detach when the journey context exits"


def test_correlation_restores_after_context_exit(spans):
    from backend.instrumentation.telemetry import (
        correlated_span, current_correlation)
    with pytest.raises(RuntimeError):
        with correlated_span("failing.step", run_id="run-x2"):
            raise RuntimeError("boom")
    assert current_correlation() == {}, \
        "context detaches even when the span body raises"


def test_journey_is_end_to_end_traceable(spans):
    """THE §20.25 acceptance: a simulated full journey — run → task →
    agent invocation → effect → browser → artifact → recovery — yields
    spans that ALL carry the same run_id, each carrying its own stage
    id, and the log stream carries the same ids."""
    from backend.instrumentation.telemetry import (
        correlated_span, CorrelationLogFilter, current_correlation)

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("agentos.journey.test")
    logger.setLevel(logging.INFO)
    handler = Capture()
    corr_filter = CorrelationLogFilter()
    handler.addFilter(corr_filter)
    logger.addHandler(handler)
    try:
        with correlated_span("run.start", run_id=run_id,
                             policy_version="react-v3"):
            logger.info("run dispatched")
            with correlated_span("task.execute", task_id="task-1"):
                logger.info("task executing")
                with correlated_span("agent.invoke", agent_id="agent-1",
                                     invocation_id="inv-1"):
                    logger.info("model invoked")
                    with correlated_span("effect.execute", effect_id="fx-1"):
                        with correlated_span("browser.session",
                                             browser_session_id="bs-1"):
                            pass
                        with correlated_span("artifact.write",
                                             artifact_id="art-1"):
                            pass
                    with correlated_span("recovery.detect",
                                         recovery_episode_id="ep-1"):
                        logger.info("recovery episode opened")
    finally:
        logger.removeHandler(handler)

    finished = spans.get_finished_spans()
    names = {s.name: s for s in finished}
    for expected in ("run.start", "task.execute", "agent.invoke",
                     "effect.execute", "browser.session", "artifact.write",
                     "recovery.detect"):
        assert expected in names, f"stage span {expected} missing"
    # Every stage carries the SAME run_id — the journey joins by id.
    for name, span in names.items():
        if name.startswith(("run.", "task.", "agent.", "effect.",
                            "browser.", "artifact.", "recovery.")):
            assert span.attributes.get("run_id") == run_id, name
    assert names["browser.session"].attributes["browser_session_id"] == "bs-1"
    assert names["recovery.detect"].attributes["recovery_episode_id"] == "ep-1"
    # Log records carry the correlation prefix of their stage.
    assert "run_id=" in records[0].correlation_prefix
    task_log = records[1].correlation_prefix
    assert "task_id=task-1" in task_log and "run_id=" in task_log


def test_sensitive_values_never_reach_span_attributes(spans):
    from backend.instrumentation.telemetry import correlated_span
    with correlated_span("model.call", run_id="run-x3",
                         attributes={"api_key": "sk-live-123",
                                     "email": "user@example.com",
                                     "prompt": "summarize this doc"}):
        pass
    span = spans.get_finished_spans()[-1]
    assert span.attributes["api_key"] == "[REDACTED]"
    assert span.attributes["email"] == "[REDACTED]"
    assert span.attributes["prompt"] == "summarize this doc"


def test_correlation_prefix_is_empty_without_journey():
    from backend.instrumentation.telemetry import correlation_log_prefix
    assert correlation_log_prefix() == ""
