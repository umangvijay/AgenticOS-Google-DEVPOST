"""Budget & usage accounting domain model (Phase 2, §2.2–2.6).

Single source of truth for budget scopes, units, reservation identities and
states, typed errors, and the budget snapshot the API and frontend consume.
Spec: docs/budget-accounting.md.

Deliberately framework-free (no FastAPI/pydantic imports) so repositories,
services, and tests can share it without coupling.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ── Units (§2.2) ──────────────────────────────────────────────────────

class BudgetUnit(str, Enum):
    """Billable unit a budget is denominated in. Phase 2 enforces tokens;
    monetary and infrastructure scopes are future work."""
    TOKENS = "tokens"
    # USD = "usd"          # deferred: needs pricing + billable-unit coverage
    # BROWSER_ACTIONS = "browser_actions"  # deferred


# ── Scopes (§2.3) ─────────────────────────────────────────────────────

class BudgetScope(str, Enum):
    """Who owns the allowance a call draws from. A call must satisfy every
    applicable scope's constraint."""
    PLATFORM = "platform"   # ceiling the service permits (admin-controlled)
    ORG = "org"             # shared organization allowance
    USER = "user"           # personal daily allowance
    RUN = "run"             # one workflow run (reserved for Phase 3)
    CALL = "call"           # one provider request (execution policy)


# ── Reservation states (§2.6) ─────────────────────────────────────────

class ReservationState(str, Enum):
    RESERVED = "reserved"          # hold committed; provider not dispatched
    DISPATCHING = "dispatching"    # dispatch intent committed (crash window)
    SETTLED = "settled"            # usage recorded, hold removed atomically
    UNCERTAIN = "uncertain"        # may have incurred usage; still counted
    RELEASED = "released"          # proven unused / reconciled to no charge


# ── Measurement sources (§2.12) ───────────────────────────────────────

class MeasurementSource(str, Enum):
    PROVIDER_REPORTED = "provider_reported"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


# ── Typed errors (§2.7) ───────────────────────────────────────────────

class BudgetErrorCode(str, Enum):
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    ACCOUNTING_UNAVAILABLE = "ACCOUNTING_UNAVAILABLE"
    RESERVATION_CONFLICT = "RESERVATION_CONFLICT"
    SETTLEMENT_CONFLICT = "SETTLEMENT_CONFLICT"
    BUDGET_POLICY_INVALID = "BUDGET_POLICY_INVALID"
    RESERVATION_NOT_FOUND = "RESERVATION_NOT_FOUND"
    RESERVATION_STATE_CONFLICT = "RESERVATION_STATE_CONFLICT"


# Suggested HTTP mapping — the router owns the final mapping (§8 of spec).
ERROR_HTTP_STATUS: Dict[BudgetErrorCode, int] = {
    BudgetErrorCode.BUDGET_EXHAUSTED: 429,
    BudgetErrorCode.ACCOUNTING_UNAVAILABLE: 503,
    BudgetErrorCode.RESERVATION_CONFLICT: 409,
    BudgetErrorCode.SETTLEMENT_CONFLICT: 409,
    BudgetErrorCode.BUDGET_POLICY_INVALID: 422,
    BudgetErrorCode.RESERVATION_NOT_FOUND: 404,
    BudgetErrorCode.RESERVATION_STATE_CONFLICT: 409,
}


class BudgetError(Exception):
    """Typed budget error. `retryable_now` is False for exhausted budgets
    that cannot admit the request this period; store outages are also
    non-retryable until accounting is healthy again."""

    def __init__(
        self,
        code: BudgetErrorCode,
        message: str,
        *,
        available: Optional[int] = None,
        required: Optional[int] = None,
        unit: BudgetUnit = BudgetUnit.TOKENS,
        retryable_now: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.available = available
        self.required = required
        self.unit = unit
        self.retryable_now = retryable_now
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "code": self.code.value,
            "unit": self.unit.value,
            "retryable_now": self.retryable_now,
            "message": self.message,
        }
        if self.available is not None:
            body["available"] = self.available
        if self.required is not None:
            body["required"] = self.required
        if self.details:
            body.update(self.details)
        return body


# ── Identities and records (§2.5) ─────────────────────────────────────

def new_reservation_id() -> str:
    """Generate outside the transaction so a crash mid-admission never
    reuses an identity (Firestore transactions may replay callbacks)."""
    return f"res_{uuid.uuid4().hex}"


def new_attempt_id() -> str:
    return f"att_{uuid.uuid4().hex}"


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


def request_digest(payload: Any) -> str:
    """Stable digest of the reservation request payload. Same key + same
    digest ⇒ replay; same key + different digest ⇒ conflict."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ReservationRequest:
    """Everything needed to admit one potentially billable attempt."""
    user_id: str
    units: int
    unit: BudgetUnit = BudgetUnit.TOKENS
    organization_id: Optional[str] = None
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    call_id: Optional[str] = None
    attempt_id: Optional[str] = None
    provider: str = "gemini"
    model: Optional[str] = None
    credential_owner_reference: Optional[str] = None
    request_key: Optional[str] = None
    purpose: str = "generation"          # generation | embedding | decision | repair
    period: Optional[str] = None         # UTC day; assigned at admission when None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def effective_request_key(self) -> str:
        if self.request_key:
            return self.request_key
        if self.call_id:
            # Deterministic key for a logical call: repeated execution of the
            # same call retrieves the same reservation (replay window).
            return request_digest({
                "user_id": self.user_id,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "call_id": self.call_id,
                "purpose": self.purpose,
            })
        # No logical call identity: every invocation is its own attempt
        # (§2.5 — a genuine new attempt gets its own identity), so two
        # model calls in one task can never collide on a shared key.
        return f"attempt|{new_attempt_id()}"

    def fingerprint(self) -> str:
        return request_digest({
            "user_id": self.user_id,
            "units": self.units,
            "unit": self.unit.value,
            "organization_id": self.organization_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "call_id": self.call_id,
            "provider": self.provider,
            "model": self.model,
            "purpose": self.purpose,
        })


@dataclass
class Reservation:
    reservation_id: str
    request_key: str
    request_digest: str
    user_id: str
    units: int
    state: ReservationState
    unit: BudgetUnit = BudgetUnit.TOKENS
    organization_id: Optional[str] = None
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    call_id: Optional[str] = None
    attempt_id: Optional[str] = None
    provider: str = "gemini"
    model: Optional[str] = None
    credential_owner_reference: Optional[str] = None
    policy_version: Optional[str] = None
    pricing_version: Optional[str] = None
    buckets: List[Dict[str, str]] = field(default_factory=list)  # charged scopes
    period: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None
    dispatch_started_at: Optional[str] = None
    provider_request_id: Optional[str] = None
    measurement_source: str = MeasurementSource.ESTIMATED.value
    settled_units: int = 0
    receipt_digest: Optional[str] = None
    record_version: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Reservation":
        buckets = data.get("buckets")
        if isinstance(buckets, str):
            try:
                buckets = json.loads(buckets)
            except (TypeError, ValueError):
                buckets = []
        return cls(
            reservation_id=data["reservation_id"],
            request_key=data.get("request_key", ""),
            request_digest=data.get("request_digest", ""),
            user_id=data["user_id"],
            units=int(data.get("units") or 0),
            state=ReservationState(data.get("state") or ReservationState.RESERVED.value),
            unit=BudgetUnit(data.get("unit") or BudgetUnit.TOKENS.value),
            organization_id=data.get("organization_id"),
            run_id=data.get("run_id"),
            task_id=data.get("task_id"),
            call_id=data.get("call_id"),
            attempt_id=data.get("attempt_id"),
            provider=data.get("provider") or "gemini",
            model=data.get("model"),
            credential_owner_reference=data.get("credential_owner_reference"),
            policy_version=data.get("policy_version"),
            pricing_version=data.get("pricing_version"),
            buckets=buckets or [],
            period=data.get("period"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            expires_at=data.get("expires_at"),
            dispatch_started_at=data.get("dispatch_started_at"),
            provider_request_id=data.get("provider_request_id"),
            measurement_source=data.get("measurement_source") or MeasurementSource.ESTIMATED.value,
            settled_units=int(data.get("settled_units") or 0),
            receipt_digest=data.get("receipt_digest"),
            record_version=int(data.get("record_version") or 0),
        )


@dataclass
class SettlementReceipt:
    """Evidence for settling a reservation. Units are the normalized actual
    usage; the repository reads the ORIGINAL hold from the reservation row —
    callers never supply release amounts (§2.7)."""
    reservation_id: str
    units: int
    unit: BudgetUnit = BudgetUnit.TOKENS
    measurement_source: MeasurementSource = MeasurementSource.PROVIDER_REPORTED
    provider_request_id: Optional[str] = None
    resolved_model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    reported_total_tokens: Optional[int] = None
    normalizer_version: Optional[str] = None
    pricing_version: Optional[str] = None
    cost_amount: Optional[float] = None
    cost_currency: Optional[str] = None
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

    def digest(self) -> str:
        return request_digest({
            "units": self.units,
            "unit": self.unit.value,
            "measurement_source": self.measurement_source.value,
            "provider_request_id": self.provider_request_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        })


@dataclass
class BudgetSnapshot:
    """Authoritative state of one budget bucket (§2.4 equation)."""
    scope: BudgetScope
    subject_id: str
    unit: BudgetUnit = BudgetUnit.TOKENS
    period: str = ""
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    limit: int = 0
    consumed: int = 0
    reserved: int = 0
    uncertain_reserved: int = 0
    available: int = 0
    accounting_status: str = "healthy"       # healthy | degraded | unavailable
    policy_version: Optional[str] = None
    provider_rate_limit: Optional[Dict[str, Any]] = None
    as_of: Optional[str] = None

    def __post_init__(self) -> None:
        # uncertain_reserved is a subset of reserved (§8 of the spec), never
        # an additional subtraction.
        self.uncertain_reserved = min(self.uncertain_reserved, self.reserved)
        self.available = self.limit - self.consumed - self.reserved
        if self.as_of is None:
            self.as_of = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "unit": self.unit.value,
            "period": self.period,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "limit": self.limit,
            "consumed": self.consumed,
            "reserved": self.reserved,
            "uncertain_reserved": self.uncertain_reserved,
            "available": self.available,
            "accounting_status": self.accounting_status,
            "policy_version": self.policy_version,
            # 30.32: provider rate-limit state ONLY when the provider
            # supplied it; absent data renders as explicitly unavailable,
            # never a guessed reset.
            "provider_rate_limit": self.provider_rate_limit,
            "as_of": self.as_of,
        }


POLICY_VERSION = "budget-policy-2"


# ── Policy (§2.3 settings semantics) ─────────────────────────────────

MAX_TOKEN_LIMIT_CEILING = 10_000_000_000  # reject absurd values


def resolve_effective_limit(
    user_limit: Optional[int],
    platform_limit: Optional[int],
    default_limit: int,
) -> int:
    """null → default; 0 → no new paid usage; negative → invalid; values
    above the platform ceiling are clamped DOWN (a user can never raise an
    administrator-controlled ceiling)."""
    if user_limit is not None:
        if user_limit < 0:
            raise BudgetError(
                BudgetErrorCode.BUDGET_POLICY_INVALID,
                "daily_token_limit cannot be negative",
            )
        if user_limit > MAX_TOKEN_LIMIT_CEILING:
            raise BudgetError(
                BudgetErrorCode.BUDGET_POLICY_INVALID,
                "daily_token_limit exceeds the maximum accepted value",
            )
    effective = user_limit if user_limit is not None else default_limit
    if platform_limit is not None and platform_limit >= 0:
        effective = min(effective, platform_limit)
    return int(effective)


def utc_period_day(now: Optional[datetime] = None) -> str:
    """The authoritative accounting period: a UTC calendar day (§2.14). The
    period is assigned when the reservation is ADMITTED and settlement stays
    against that original period even if the response lands after midnight."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def period_bounds(period: str) -> (str, str):
    """UTC ISO bounds of a period day — used for display; the UI converts to
    the user's timezone."""
    start = f"{period}T00:00:00Z"
    return start, start


# ── Accounting events (append-only, §2.7/§2.19) ──────────────────────

class BudgetEventType(str, Enum):
    RESERVATION_CREATED = "reservation_created"
    DISPATCH_MARKED = "dispatch_marked"
    SETTLED = "settled"
    RELEASED = "released"
    MARKED_UNCERTAIN = "marked_uncertain"
    RECONCILED = "reconciled"
    ADJUSTMENT = "adjustment"                # authorized correction
    LEGACY_SEED = "legacy_seed"              # migration: pre-existing aggregates


@dataclass
class BudgetEvent:
    event_id: str
    event_type: BudgetEventType
    reservation_id: Optional[str]
    scope: BudgetScope
    subject_id: str
    period: str
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
