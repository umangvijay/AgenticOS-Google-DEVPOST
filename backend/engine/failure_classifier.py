"""Deterministic failure classification for self-healing tool execution.

Distilled from the MCPForge prototype: cheap, deterministic classification
from status codes and exception text — never model judgement. Every failed
tool call carries a classification so the orchestrator can self-correct
(different arguments, refreshed credential, or a different tool) instead of
blindly retrying the identical broken call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class FailureClassification:
    category: str                 # transient_network | rate_limited | auth_expired | auth_invalid |
                                  # schema_mismatch | not_found | server_error | timeout |
                                  # timeout_before_dispatch | ack_lost | stale_page | internal
    retryable: bool               # safe to auto-retry the identical call
    max_auto_attempts: int        # retry budget for this category
    recovery_hint: str            # what the agent (or user) should do next


def classify_failure(message: str = "", status: Optional[int] = None,
                     dispatched: Optional[bool] = None) -> FailureClassification:
    """Classify a failure from its observable surface.

    ``dispatched`` (Phase 30 / 30.28) distinguishes a timeout BEFORE the
    provider call left the process (retry may be safe — the hold release
    still requires proof dispatch never occurred) from an acknowledgement
    lost AFTER a write was dispatched (the identical call must NOT be
    repeated — reconcile by stable request id / destination state)."""
    msg = (message or "").lower()

    if any(s in msg for s in (
        "stale", "human takeover", "controller generation", "owner generation",
        "document generation", "page changed", "frame changed", "target changed",
    )):
        return FailureClassification(
            "stale_page", False, 0,
            "The observation is stale or control changed hands. Discard the action, "
            "wait for current authority and take a FRESH observation before acting.",
        )
    if any(s in msg for s in (
        "acknowledgement lost", "ack lost", "response lost", "no response after write",
        "dispatch confirmed but", "unknown outcome", "uncertain write",
    )):
        return FailureClassification(
            "ack_lost", False, 0,
            "A write was dispatched but its outcome is unknown. Do NOT resend; reconcile "
            "by the stable request/idempotency reference or read the destination state.",
        )

    if status is not None:
        if status == 401 or status == 403:
            return FailureClassification(
                "auth_invalid", False, 0,
                "The credential was rejected (HTTP %d). Attach a fresh API key for this "
                "integration on the Integrations page or in Vault, then call the tool again." % status,
            )
        if status == 404:
            return FailureClassification(
                "not_found", False, 0,
                "Endpoint or resource not found (HTTP 404). Re-check the path/identifier — "
                "try listing resources first to discover valid ids, or use a different endpoint.",
            )
        if status == 422 or status == 400:
            return FailureClassification(
                "schema_mismatch", False, 0,
                "The API rejected the arguments (HTTP %d). Read the error body and adjust the "
                "parameters — do not resend unchanged arguments." % status,
            )
        if status == 429:
            return FailureClassification(
                "rate_limited", True, 2,
                "Rate limited (HTTP 429). Wait briefly before retrying; consider a cheaper call.",
            )
        if 500 <= status <= 599:
            return FailureClassification(
                "server_error", True, 2,
                "Upstream server error (HTTP %d). Retry once or twice; if it persists, "
                "try another endpoint or report the outage." % status,
            )

    if any(s in msg for s in ("timed out", "timeout", "deadline")):
        if dispatched is True:
            # The call left the process before the deadline hit — a retry
            # could double a write. Reconciliation owns this outcome.
            return FailureClassification(
                "ack_lost", False, 0,
                "The call timed out AFTER dispatch. Do NOT resend; reconcile by the "
                "stable request reference or the destination state.",
            )
        if dispatched is False:
            return FailureClassification(
                "timeout_before_dispatch", True, 2,
                "The call timed out before dispatch. Retry within the deadline and "
                "budget; release the unused hold only with proof dispatch never occurred.",
            )
        return FailureClassification(
            "timeout", True, 2,
            "The call timed out. Retry; if it times out again, try a smaller request.",
        )
    if any(s in msg for s in (
        "429", "quota", "resource_exhausted", "unavailable", "overloaded",
        "connection", "econnreset", "network", "temporarily",
    )):
        return FailureClassification(
            "transient_network" if "quota" not in msg else "rate_limited",
            True, 3,
            "Transient network/quota issue. Retry with a short pause; switch endpoints if it repeats.",
        )
    if any(s in msg for s in ("captcha", "mfa", "otp", "login", "sign in")):
        return FailureClassification(
            "auth_expired", False, 0,
            "The site requires an interactive login or verification. Ask the user to complete "
            "it (store credentials in Vault) or use browser tools with a stored credential.",
        )
    if any(s in msg for s in ("ssrf", "unauthorized", "401", "403", "forbidden")):
        return FailureClassification(
            "auth_invalid", False, 0,
            "Access denied. Check the credential for this integration and the URL allowlist.",
        )
    if any(s in msg for s in ("unknown tool", "missing tool", "no integration", "not registered")):
        return FailureClassification(
            "internal", False, 0,
            "That tool is not available. Build the integration first (build_integration), "
            "then call its tools from the refreshed catalog.",
        )
    return FailureClassification(
        "internal", False, 0,
        "Unexpected failure. Inspect the error, try a different approach or tool.",
    )
