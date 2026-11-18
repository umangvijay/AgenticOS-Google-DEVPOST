"""Specialist failure recovery (Phase 15 §15.17) — classify BEFORE
choosing a response. Restarting a specialist must never blindly resend
an email, recreate a record, or repeat an uncertain write: the class
determines the response, and effect-bearing classes route through
reconciliation first (§5).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class FailureClass(str, Enum):
    TEMPORARY_READ = "temporary_read_failure"
    INVALID_OUTPUT = "invalid_structured_output"
    MISSING_INPUT = "missing_input"
    EXPIRED_CREDENTIALS = "expired_credentials"
    AUTHORIZATION = "authorization_failure"
    UNCERTAIN_WRITE = "uncertain_external_write"
    WORKER_LOSS = "worker_loss"
    REPEATED_EQUIVALENT = "repeated_equivalent_failure"


class Response(str, Enum):
    BOUNDED_RETRY = "bounded_retry"
    BOUNDED_CORRECTION = "bounded_correction"
    REQUEST_INPUT = "request_specific_input"
    RECONNECT = "preserve_work_and_reconnect"
    STOP_EXECUTION = "stop_affected_execution"
    RECONCILE_FIRST = "reconcile_before_replay"
    RECLAIM = "reclaim_with_new_generation"
    ESCALATE = "escalate_with_evidence"


# §15.17: the classification table — the ONLY response a failure class
# may produce.
CLASS_TABLE: Dict[FailureClass, Response] = {
    FailureClass.TEMPORARY_READ: Response.BOUNDED_RETRY,
    FailureClass.INVALID_OUTPUT: Response.BOUNDED_CORRECTION,
    FailureClass.MISSING_INPUT: Response.REQUEST_INPUT,
    FailureClass.EXPIRED_CREDENTIALS: Response.RECONNECT,
    FailureClass.AUTHORIZATION: Response.STOP_EXECUTION,
    FailureClass.UNCERTAIN_WRITE: Response.RECONCILE_FIRST,
    FailureClass.WORKER_LOSS: Response.RECLAIM,
    FailureClass.REPEATED_EQUIVALENT: Response.ESCALATE,
}


@dataclass
class RecoveryDecision:
    failure_class: FailureClass
    response: Response
    detail: str
    # Whether the response may RE-EXECUTE the original action. Effect-
    # bearing responses (reconcile) re-execute only AFTER evidence.
    may_reexecute: bool = False


def classify_specialist_failure(*, error_code: str = "",
                                error_text: str = "",
                                attempts: int = 1,
                                effect_state: Optional[str] = None,
                                output_schema_violations: Optional[int] = None,
                                missing_input_keys: Optional[list] = None,
                                worker_alive: bool = True,
                                ) -> RecoveryDecision:
    """Classify a specialist failure and return its ONLY permitted
    response. Ordering matters: authorization and uncertain writes
    (safety) are checked before transient classes."""
    text = (error_text or "").lower()
    code = (error_code or "").lower()
    if effect_state in ("UNKNOWN", "OUTCOME_UNKNOWN", "PARTIALLY_APPLIED"):
        # §5/§15.17: an uncertain external write is NEVER retried blind.
        return RecoveryDecision(
            FailureClass.UNCERTAIN_WRITE, Response.RECONCILE_FIRST,
            "the external effect is uncertain — reconcile before any "
            "replay", may_reexecute=False)
    if "authorization" in code or "authorization" in text or \
            "forbidden" in code or "access" in code and "lost" in text:
        return RecoveryDecision(
            FailureClass.AUTHORIZATION, Response.STOP_EXECUTION,
            "authorization failure — stop the affected execution")
    if output_schema_violations:
        return RecoveryDecision(
            FailureClass.INVALID_OUTPUT, Response.BOUNDED_CORRECTION,
            f"{output_schema_violations} output-schema violation(s) — "
            "bounded correction request")
    if missing_input_keys:
        return RecoveryDecision(
            FailureClass.MISSING_INPUT, Response.REQUEST_INPUT,
            "missing input: " + ", ".join(map(str, missing_input_keys)))
    if "credential" in code or "expired" in text and "token" in text:
        return RecoveryDecision(
            FailureClass.EXPIRED_CREDENTIALS, Response.RECONNECT,
            "credentials expired — preserve work and reconnect")
    if not worker_alive:
        return RecoveryDecision(
            FailureClass.WORKER_LOSS, Response.RECLAIM,
            "the worker died — reclaim with a new generation")
    if attempts >= 3:
        return RecoveryDecision(
            FailureClass.REPEATED_EQUIVALENT, Response.ESCALATE,
            f"{attempts} equivalent failures — escalate with evidence")
    return RecoveryDecision(
        FailureClass.TEMPORARY_READ, Response.BOUNDED_RETRY,
        "temporary failure — bounded retry")
