"""
AgentOS — Distributed Tracing Configuration

Configures OpenTelemetry for W3C Trace Context propagation across the agentic workflow.
"""

import logging
from opentelemetry.propagate import set_global_textmap
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from backend.instrumentation.telemetry import trace_provider

logger = logging.getLogger(__name__)

_initialized = False

def init_tracing(app_name: str = "AgentOS"):
    """Initialize OpenTelemetry tracing with W3C Trace Context."""
    global _initialized
    if _initialized:
        return

    # Enable W3C Trace Context
    set_global_textmap(TraceContextTextMapPropagator())
    
    _initialized = True
    logger.info("Distributed Tracing Initialized (W3C Trace Context)")

def get_tracer(name: str):
    """Get a tracer instance for the current module."""
    if not _initialized:
        init_tracing()
    return trace_provider.get_tracer(name)
