"""
AgentOS — Advanced Rate Limiter (Phase 3 §3.3/§3.8/§3.9)

Two enforcement layers, resolved through ONE policy inventory
(backend/security/admission_policy.py):

1. In-process sliding-window counter — fast, per-replica, authoritative for
   high-volume groups (`general_read`, `event_stream`).
2. Shared durable fixed-window counter — cross-replica, authoritative for
   admission-sensitive groups (auth, contact, webhook, mcp_build, workflow…).

Every denial carries a typed AdmissionDecision (reason code, enforcement
mode, retry timing) so the backend can distinguish "user exhausted a healthy
quota" (429) from "admission infrastructure is down" (503), and the frontend
can render the right state.
"""

import time
import logging
from collections import defaultdict
from typing import Optional, Dict, Tuple
from threading import Lock

from fastapi import HTTPException, Request, status

from backend.config.settings import settings
from backend.models.admission import (
    AdmissionDecision,
    AdmissionPolicyError,
    AdmissionReason,
)
from backend.security.admission_policy import resolve_policy, RATE_LIMIT_GROUPS

logger = logging.getLogger(__name__)


class SlidingWindowCounter:
    """
    Sliding window rate limiter.

    Instead of fixed windows (which allow bursts at boundaries),
    this interpolates between the current and previous window to give
    a smooth rate limit.
    """

    def __init__(self, max_keys: int = 10000):
        self._lock = Lock()
        self._max_keys = max_keys
        self._next_cleanup = 0.0
        # Key: (identifier, group) → {window_start, prev_count, curr_count}
        self._windows: Dict[Tuple[str, str], Dict] = defaultdict(
            lambda: {"window_start": 0, "prev_count": 0, "curr_count": 0}
        )

    def check_rate_limit(
        self,
        identifier: str,
        group: str,
        max_requests: int,
        window_seconds: int = 60,
    ) -> Tuple[bool, dict]:
        """
        Check if a request is allowed.

        Returns:
            (allowed, info_dict) where info_dict contains:
            - limit: max requests
            - remaining: requests remaining
            - reset: seconds until window resets
            - retry_after: seconds to wait (only if blocked)
        """
        now = time.time()
        key = (identifier, group)

        with self._lock:
            if now >= self._next_cleanup:
                stale = [key for key, state in self._windows.items() if state.get("expires_at", 0) <= now]
                for stale_key in stale:
                    del self._windows[stale_key]
                self._next_cleanup = now + 60
            if key not in self._windows and len(self._windows) >= self._max_keys:
                return False, {"limit": max_requests, "remaining": 0, "reset": 60, "retry_after": 60}
            state = self._windows[key]
            state["expires_at"] = now + 2 * window_seconds
            window_start = state["window_start"]
            elapsed = now - window_start

            if elapsed >= window_seconds:
                # Move to new window
                if elapsed >= 2 * window_seconds:
                    # Fully expired — reset both
                    state["prev_count"] = 0
                else:
                    state["prev_count"] = state["curr_count"]
                state["curr_count"] = 0
                state["window_start"] = now - (elapsed % window_seconds)
                elapsed = now - state["window_start"]

            # Sliding window estimate
            weight = 1.0 - (elapsed / window_seconds)
            estimated = state["prev_count"] * weight + state["curr_count"]

            if estimated >= max_requests:
                retry_after = window_seconds - elapsed
                return False, {
                    "limit": max_requests,
                    "remaining": 0,
                    "reset": int(retry_after),
                    "retry_after": int(retry_after) + 1,
                }

            # Allow and increment
            state["curr_count"] += 1
            remaining = max(0, int(max_requests - estimated - 1))

            return True, {
                "limit": max_requests,
                "remaining": remaining,
                "reset": int(window_seconds - elapsed),
            }


# ── Singleton ─────────────────────────────────────────────────────
_limiter = SlidingWindowCounter()


# ── HTTP mapping (§3.8) ───────────────────────────────────────────

def _http_error(decision: AdmissionDecision) -> HTTPException:
    """429 for a healthy quota denial, 503 when admission itself is
    unavailable (§3.9) — never present an outage as the user's exhaustion."""
    if decision.reason == AdmissionReason.ADMISSION_UNAVAILABLE:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        code = status.HTTP_429_TOO_MANY_REQUESTS
    return HTTPException(
        status_code=code,
        detail=decision.to_detail(),
        headers=decision.headers(),
    )


def _local_decision(identifier: str, policy) -> AdmissionDecision:
    """Run the per-process sliding window and shape its result as a typed
    decision. Raises the mapped HTTPException on local denial."""
    allowed, info = _limiter.check_rate_limit(
        identifier, policy.group, policy.limit, policy.window_seconds
    )
    reset_at = int(time.time()) + int(info.get("reset", policy.window_seconds))
    decision = AdmissionDecision(
        allowed=allowed,
        reason=AdmissionReason.ALLOWED if allowed else AdmissionReason.RATE_LIMITED,
        enforcement=enforcement_for_local(policy),
        policy_group=policy.group,
        policy_version=policy.policy_version,
        limit=info.get("limit", policy.limit),
        remaining=info.get("remaining", 0),
        retry_after_seconds=int(info.get("retry_after", 0)) if not allowed else 0,
        reset_at_epoch=reset_at,
    )
    if not allowed:
        logger.warning(
            "Local rate limit exceeded: %s on %s (limit=%d/%ds)",
            identifier, policy.group, policy.limit, policy.window_seconds,
        )
        raise _http_error(decision)
    return decision


def enforcement_for_local(policy):
    """Which mode label a local-layer decision carries. The local limiter is
    the DESIGNED layer for non-shared groups; for shared groups it only ever
    appears alongside (or during the degraded emergency of) shared checks."""
    from backend.models.admission import EnforcementMode
    return EnforcementMode.LOCAL_ONLY if not policy.shared else EnforcementMode.LOCAL_EMERGENCY


def check_rate_limit(
    identifier: str,
    group: str = "general",
    window_seconds: int = 60,
) -> dict:
    """
    In-process sliding-window check for an identifier (user_id or IP).
    Raises HTTPException(429) if limit exceeded. Returns rate limit info.

    Unknown groups raise AdmissionPolicyError — configuration errors must be
    loud, not silently absorbed into the general allowance (§3.3).
    """
    policy = resolve_policy(group)
    decision = _local_decision(identifier, policy)
    return {"limit": decision.limit, "remaining": decision.remaining}


def get_client_identifier(request: Request, user_id: Optional[str] = None) -> str:
    """
    Get a rate limit identifier.
    Prefer user_id if authenticated, fall back to client IP.
    """
    if user_id:
        return f"user:{user_id}"

    # ASGI server middleware owns trusted-proxy parsing. Raw forwarded headers
    # are caller-controlled and must never replace the authenticated peer here.
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


# ── Shared admission (§3.8/§3.9) ──────────────────────────────────

async def admission_decision(identifier: str, group: str) -> AdmissionDecision:
    """Typed shared-store admission for one identity and policy group.

    Returns the decision WITHOUT raising (callers that want exceptions use
    enforce/check_shared_rate_limit). Includes degraded-mode labeling so
    metrics and logs can distinguish authoritative from emergency results.
    """
    from backend.security.shared_rate_limiter import shared_admission
    policy = resolve_policy(group)
    decision = await shared_admission(identifier, policy.group, policy=policy)
    if decision.degraded:
        logger.warning(
            "Admission degraded for %s on %s (mode=%s reason=%s)",
            identifier, policy.group, decision.enforcement.value, decision.reason.value,
        )
    return decision


async def enforce(identifier: str, group: str) -> AdmissionDecision:
    """The ONE call a protected route makes: local layer + shared layer,
    policy-driven, typed result. Denials raise HTTPException carrying the
    structured decision body (§3.19 contract) and standard headers."""
    policy = resolve_policy(group)
    _local_decision(identifier, policy)   # raises 429 on local denial
    decision = await admission_decision(identifier, group)
    if not decision.allowed:
        raise _http_error(decision)
    return decision


async def check_shared_rate_limit(identifier: str, group: str = "general",
                                  window_seconds: int = 60) -> dict:
    """Compat wrapper (pre-Phase-3 signature). Shared-store-only admission;
    the local layer stays a separate check as before. Raises the structured
    HTTPException on denial."""
    decision = await admission_decision(identifier, group)
    if not decision.allowed:
        raise _http_error(decision)
    return {
        "limit": decision.limit,
        "remaining": decision.remaining,
        "shared": decision.enforcement.value == "shared",
    }
