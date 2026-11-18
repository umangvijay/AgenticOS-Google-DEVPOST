"""Admission & capacity domain model (Phase 3, §3.3 / §3.8 / §3.11).

Single source of truth for admission decisions, reason codes, enforcement
modes, policy descriptors, and capacity leases. Deliberately framework-free
(no FastAPI/pydantic imports) so security helpers, repositories, services,
and tests share it without coupling. Spec: docs/admission-and-capacity.md.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


# ── Reason codes (§3.8) ────────────────────────────────────────────────

class AdmissionReason(str, Enum):
    """Machine-readable outcome of an admission check.

    ALLOWED is the only 'yes'. Everything else names a distinct condition the
    backend, frontend, and metrics must be able to tell apart — an exhausted
    quota (user's own doing) is not an infrastructure outage (operator's
    problem) and is not a configuration bug (developer's problem)."""

    ALLOWED = "ALLOWED"
    RATE_LIMITED = "RATE_LIMITED"                        # request-rate window exhausted
    TENANT_CAPACITY_EXHAUSTED = "TENANT_CAPACITY_EXHAUSTED"  # active lease cap reached
    QUEUE_FULL = "QUEUE_FULL"                            # pending-command cap reached
    PROVIDER_THROTTLED = "PROVIDER_THROTTLED"            # upstream provider limiting
    ADMISSION_UNAVAILABLE = "ADMISSION_UNAVAILABLE"      # store down + fail-closed policy
    POLICY_INVALID = "POLICY_INVALID"                    # misconfigured policy/inputs
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"        # same scoped key, different payload (§4.19)


# ── Enforcement modes (§3.8) ───────────────────────────────────────────

class EnforcementMode(str, Enum):
    """Which layer actually produced the decision."""

    SHARED = "shared"                    # durable cross-replica store decided
    LOCAL_ONLY = "local_only"            # deployment runs without a shared store
    LOCAL_EMERGENCY = "local_emergency"  # shared store configured but errored
    DISABLED_BY_POLICY = "disabled_by_policy"  # limit explicitly <= 0 by policy


# ── Failure modes (§3.9) ───────────────────────────────────────────────

class StoreFailureMode(str, Enum):
    """Per-workload behavior when the shared admission store is unavailable."""

    FAIL_OPEN_LOCAL = "fail_open_local"  # bounded per-process fallback admits
    FAIL_CLOSED = "fail_closed"          # reject with ADMISSION_UNAVAILABLE (503)


# ── Algorithms (§3.6) ──────────────────────────────────────────────────

class RateAlgorithm(str, Enum):
    FIXED_WINDOW = "fixed_window"        # current shared-store implementation
    SLIDING_WINDOW = "sliding_window"    # current in-process implementation


# ── Identity scopes (§3.7) ─────────────────────────────────────────────

class IdentityScope(str, Enum):
    USER = "user"
    ORG = "org"
    IP = "ip"            # anonymous/ingress identity — pair with account limits
    CREDENTIAL = "credential"  # provider quota owner
    SYSTEM = "system"    # internal jobs with an explicit service identity


# ── Typed errors ───────────────────────────────────────────────────────

class AdmissionPolicyError(Exception):
    """A policy or its inputs are invalid (negative limit, unknown group,
    malformed identity). Never degrades to an implicit 'allow everything'."""

    def __init__(self, message: str, group: Optional[str] = None):
        super().__init__(message)
        self.group = group


class ProviderThrottledError(Exception):
    """Provider throughput admission refused (§3.16). Carries retry timing
    so the caller can surface PROVIDER_THROTTLED to the frontend with real
    retry guidance instead of inventing one."""

    def __init__(self, message: str, retry_after_seconds: int = 0):
        super().__init__(message)
        self.retry_after_seconds = max(0, int(retry_after_seconds))


# ── Policy descriptor (§3.3) ───────────────────────────────────────────

@dataclass(frozen=True)
class AdmissionPolicy:
    """One row of the authoritative route/workload policy inventory."""

    group: str                        # registry key, e.g. "auth_login"
    limit: int                        # sustained allowance per window
    window_seconds: int = 60
    algorithm: RateAlgorithm = RateAlgorithm.FIXED_WINDOW
    identity: IdentityScope = IdentityScope.USER
    shared: bool = True               # enforce across replicas in the durable store
    on_store_failure: StoreFailureMode = StoreFailureMode.FAIL_OPEN_LOCAL
    http_status_denied: int = 429
    policy_version: str = "p3.v1"     # which configuration produced decisions

    def validate(self) -> "AdmissionPolicy":
        if self.limit < 0:
            raise AdmissionPolicyError(
                f"Policy {self.group!r}: limit must be >= 0 (got {self.limit})", self.group)
        if self.window_seconds <= 0:
            raise AdmissionPolicyError(
                f"Policy {self.group!r}: window_seconds must be > 0 (got {self.window_seconds})",
                self.group)
        if not self.group or not self.group.strip():
            raise AdmissionPolicyError("Policy group must be a non-empty string")
        return self

    @property
    def disabled(self) -> bool:
        """limit == 0 is a REAL, explicit policy: admit nothing. It is not
        'unlimited' and not 'missing' — the magic-number ambiguity §3.5 bans."""
        return self.limit == 0


# ── Admission decision (§3.8) ──────────────────────────────────────────

@dataclass
class AdmissionDecision:
    """The one typed answer every admission path returns or raises with."""

    allowed: bool
    reason: AdmissionReason = AdmissionReason.ALLOWED
    enforcement: EnforcementMode = EnforcementMode.LOCAL_ONLY
    policy_group: str = "general"
    policy_version: str = "p3.v1"
    limit: int = 0
    remaining: int = 0
    retry_after_seconds: int = 0
    reset_at_epoch: int = 0            # absolute epoch when the window resets
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    degraded: bool = False             # True when enforcement is not authoritative

    def headers(self) -> Dict[str, str]:
        """Standard rate-limit response headers (RFC-wise, stable contract)."""
        h = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-Admission-Mode": self.enforcement.value,
            "X-Admission-Decision-Id": self.decision_id,
        }
        if self.reset_at_epoch:
            h["X-RateLimit-Reset"] = str(self.reset_at_epoch)
        if not self.allowed and self.retry_after_seconds > 0:
            h["Retry-After"] = str(self.retry_after_seconds)
        return h

    def to_detail(self) -> Dict[str, Any]:
        """Structured HTTP error body — the frontend contract (§3.19)."""
        return {
            "code": self.reason.value,
            "message": _REASON_MESSAGES.get(self.reason, self.reason.value),
            "group": self.policy_group,
            "retry_after_seconds": self.retry_after_seconds,
            "accepted": False,
            "enforcement": self.enforcement.value,
            "decision_id": self.decision_id,
        }


_REASON_MESSAGES: Dict[AdmissionReason, str] = {
    AdmissionReason.ALLOWED: "Allowed.",
    AdmissionReason.RATE_LIMITED: "Too many requests in the current interval.",
    AdmissionReason.TENANT_CAPACITY_EXHAUSTED:
        "Your workspace has reached its active capacity limit.",
    AdmissionReason.QUEUE_FULL:
        "Your workspace has too much work waiting; retry after some completes.",
    AdmissionReason.PROVIDER_THROTTLED:
        "An external provider is limiting execution; retrying shortly.",
    AdmissionReason.ADMISSION_UNAVAILABLE:
        "AgentOS cannot safely decide whether to accept work right now.",
    AdmissionReason.POLICY_INVALID:
        "Admission policy is misconfigured; the request was refused.",
}


# ── Capacity leases (§3.11) ────────────────────────────────────────────

class ResourceClass(str, Enum):
    """What a lease exclusively counts. Each protects a different resource
    (§3.2) — one number must never stand in for another."""

    WORKFLOW_TASK = "workflow_task"
    MCP_BUILD = "mcp_build"
    BROWSER_SESSION = "browser_session"
    PROVIDER_CALL = "provider_call"
    ARTIFACT_GENERATION = "artifact_generation"


class LeaseState(str, Enum):
    ACTIVE = "active"          # counted against capacity
    RELEASED = "released"      # normal completion (idempotent terminal state)
    EXPIRED = "expired"        # TTL passed; capacity recovered by reconciliation
    ORPHANED = "orphaned"      # released by a newer generation (fenced)


@dataclass
class CapacityLease:
    lease_id: str
    resource_class: ResourceClass
    scope_key: str                     # tenant the cap applies to (org or user)
    owner_id: str                      # authenticated principal that caused the work
    execution_key: str                 # idempotency: command/task/attempt identity
    worker_id: str = ""
    units: int = 1
    generation: int = 1                # fencing token; stale workers lose rights
    acquired_at_epoch: int = 0
    expires_at_epoch: int = 0
    last_heartbeat_epoch: int = 0
    state: LeaseState = LeaseState.ACTIVE
    organization_id: Optional[str] = None
    command_id: Optional[str] = None
    task_id: Optional[str] = None
    attempt_id: Optional[str] = None


class CapacityError(Exception):
    """Base class for typed capacity failures."""


class CapacityExhaustedError(CapacityError):
    """The tenant's active-unit cap for the resource class is reached."""


class LeaseConflictError(CapacityError):
    """Renewal/release attempted by a stale generation — fenced out (§3.11)."""


def new_lease_id() -> str:
    return f"lease_{uuid.uuid4().hex[:20]}"


def epoch_now() -> int:
    return int(time.time())
