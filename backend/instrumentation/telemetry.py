import logging
import os
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, ConsoleMetricExporter
from typing import Any, Dict

# Set up global OpenTelemetry providers
resource = Resource(attributes={"service.name": "agentic-os"})

# A console exporter is useful while explicitly debugging locally, but it can
# leak prompt/workflow metadata into logs. Production must use a reviewed OTLP
# pipeline; never silently dump spans or metrics to stdout.
_console_export = (
    os.getenv("APP_ENV", "development").lower() != "production"
    and os.getenv("OTEL_CONSOLE_EXPORT", "0").lower() in {"1", "true", "yes"}
)

# Tracing
trace_provider = TracerProvider(resource=resource)
if _console_export:
    trace_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer("agentic-os.tracer")

# Metrics
metric_readers = (
    [PeriodicExportingMetricReader(ConsoleMetricExporter())] if _console_export else []
)
meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("agentic-os.meter")

class TelemetrySanitizer:
    """Sanitizes trace attributes to prevent leaking secrets and PII."""
    
    SENSITIVE_KEYS = {
        "api_key", "token", "password", "secret", "authorization",
        "jwt", "credentials", "email", "ssn", "phone", "_approved_request_id"
    }

    @classmethod
    def sanitize(cls, data: Any) -> Any:
        if isinstance(data, dict):
            sanitized = {}
            for k, v in data.items():
                if any(sensitive in k.lower() for sensitive in cls.SENSITIVE_KEYS):
                    sanitized[k] = "[REDACTED]"
                else:
                    sanitized[k] = cls.sanitize(v)
            return sanitized
        elif isinstance(data, list):
            return [cls.sanitize(item) for item in data]
        # Avoid logging massive strings (e.g. file contents or base64 data)
        elif isinstance(data, str) and len(data) > 2000:
            return data[:1000] + "... [TRUNCATED]"
        return data

def get_tracer():
    return tracer

def get_meter():
    return meter


# ═══════════════════════════════════════════════════════════════════════
# §20.25: journey correlation — every id of a user journey (run, task,
# agent, invocation, effect, browser session, artifact, recovery episode,
# policy version) rides on the active OTel context (baggage) so ALL spans
# and log records of that journey carry the same identity. Spans are
# exported with the ids as attributes; logs inject them via the filter
# below; W3C trace-context propagation (observability/tracing.py) carries
# them across services.
# ═══════════════════════════════════════════════════════════════════════

from contextlib import contextmanager
from contextvars import copy_context
from opentelemetry import baggage
from opentelemetry import context as otel_context
from opentelemetry.trace import SpanKind

CORRELATION_KEYS = (
    "run_id", "task_id", "agent_id", "invocation_id", "effect_id",
    "browser_session_id", "artifact_id", "recovery_episode_id",
    "policy_version",
)


def current_correlation() -> Dict[str, str]:
    """The correlation ids attached to the CURRENT context (baggage)."""
    out = {}
    for key in CORRELATION_KEYS:
        value = baggage.get_baggage(key)
        if value:
            out[key] = str(value)
    return out


def correlation_log_prefix() -> str:
    """`[run=… task=…]` prefix for log lines; '' when nothing is set."""
    corr = current_correlation()
    if not corr:
        return ""
    return "[" + " ".join(f"{k}={v}" for k, v in sorted(corr.items())) + "] "


@contextmanager
def correlated_span(name: str, *, run_id: str = "", task_id: str = "",
                    agent_id: str = "", invocation_id: str = "",
                    effect_id: str = "", browser_session_id: str = "",
                    artifact_id: str = "", recovery_episode_id: str = "",
                    policy_version: str = "", attributes: Dict[str, Any] = None,
                    kind: Any = SpanKind.INTERNAL):
    """Open a span that (a) records every provided journey id as an
    attribute and (b) ATTACHES them to the context so nested spans, log
    records, and outbound messages inherit the same journey identity.
    Values are sanitized — no secrets or PII on telemetry."""
    provided = {
        "run_id": run_id, "task_id": task_id, "agent_id": agent_id,
        "invocation_id": invocation_id, "effect_id": effect_id,
        "browser_session_id": browser_session_id,
        "artifact_id": artifact_id,
        "recovery_episode_id": recovery_episode_id,
        "policy_version": policy_version,
    }
    ids = {k: v for k, v in provided.items() if v}
    ctx = otel_context.get_current()
    # Inherit the journey ids already on the context (parent stages) so
    # EVERY span of a journey carries the same run_id etc.; ids provided
    # here take precedence.
    inherited = {}
    for key in CORRELATION_KEYS:
        val = baggage.get_baggage(key, context=ctx)
        if val:
            inherited[key] = str(val)
    ids = {**inherited, **ids}
    for k, v in ids.items():
        ctx = baggage.set_baggage(k, v, context=ctx)
    attrs = TelemetrySanitizer.sanitize(attributes or {})
    attrs.update(ids)
    token = otel_context.attach(ctx)
    try:
        with tracer.start_as_current_span(name, attributes=attrs,
                                          kind=kind) as span:
            yield span
    finally:
        otel_context.detach(token)


class CorrelationLogFilter(logging.Filter):
    """Injects the current journey's correlation ids into every record
    (`record.correlation_prefix`) — logs are joinable to traces by id."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_prefix = correlation_log_prefix()
        return True
