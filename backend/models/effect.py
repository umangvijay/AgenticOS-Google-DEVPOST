"""External-effect domain model (Phase 5, §5.2/§5.4/§5.9).

The identity and ledger for actions that change state OUTSIDE AgentOS's
database. Core promise (§5.0): AgentOS records each intended external
action, preserves uncertainty when its outcome is unknown, and retries or
resolves it only under a verified operation-specific policy.

Identity rules (§5.2):
- A transport retry preserves effect_id; a new attempt gets a new attempt id.
- A deliberate second send/create gets a NEW effect id.
- A workflow restart preserves existing effect identities.
- A provider fallback must not repeat an uncertain mutation via another channel.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


def new_effect_id() -> str:
    return "fx_" + uuid.uuid4().hex[:24]


def new_effect_attempt_id() -> str:
    return "fxa_" + uuid.uuid4().hex[:24]


def new_reconciliation_id() -> str:
    return "rcn_" + uuid.uuid4().hex[:20]


class EffectState(str, Enum):
    """§5.4 — provider-execution outcome is SEPARATE from user cancellation
    intent, reconciliation job status, and UI notification state (an email
    may be accepted by the mail server while its command is cancelled)."""

    PREPARED = "PREPARED"                      # recorded; no dispatch intent
    WAITING_APPROVAL = "WAITING_APPROVAL"      # authorization incomplete
    DISPATCHING = "DISPATCHING"                # intent committed; remote may occur
    PROVIDER_PENDING = "PROVIDER_PENDING"      # provider accepted async work
    CONFIRMED_APPLIED = "CONFIRMED_APPLIED"    # evidence confirms the effect
    CONFIRMED_NOT_APPLIED = "CONFIRMED_NOT_APPLIED"
    PARTIALLY_APPLIED = "PARTIALLY_APPLIED"    # only part of the effect occurred
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"        # evidence cannot determine it
    CANCELLED_BEFORE_DISPATCH = "CANCELLED_BEFORE_DISPATCH"
    CLOSED_UNRESOLVED = "CLOSED_UNRESOLVED"    # closed without claiming outcome


# §5.4/§5.10: OUTCOME_UNKNOWN is NOT terminal — it is precisely the state
# evidence-based reconciliation investigates. Terminal = a confirmed outcome,
# an explicit close, or a pre-dispatch cancellation.
TERMINAL_EFFECT_STATES = frozenset({
    EffectState.CONFIRMED_APPLIED, EffectState.CONFIRMED_NOT_APPLIED,
    EffectState.PARTIALLY_APPLIED,
    EffectState.CANCELLED_BEFORE_DISPATCH, EffectState.CLOSED_UNRESOLVED,
})


class EffectStateVersion:
    """Effects carry a monotonic version for conditional resolutions (§5.11)
    — two resolvers reading the same uncertain state cannot both win."""


# Allowed transitions (§5.4): conservative — uncertainty is only ever
# replaced by EVIDENCE, never by another guess.
ALLOWED_EFFECT_TRANSITIONS: Dict[EffectState, frozenset] = {
    EffectState.PREPARED: frozenset({
        EffectState.WAITING_APPROVAL, EffectState.DISPATCHING,
        EffectState.CANCELLED_BEFORE_DISPATCH,
    }),
    EffectState.WAITING_APPROVAL: frozenset({
        EffectState.DISPATCHING, EffectState.CANCELLED_BEFORE_DISPATCH,
    }),
    EffectState.DISPATCHING: frozenset({
        EffectState.PROVIDER_PENDING, EffectState.CONFIRMED_APPLIED,
        EffectState.CONFIRMED_NOT_APPLIED, EffectState.PARTIALLY_APPLIED,
        EffectState.OUTCOME_UNKNOWN,
    }),
    EffectState.PROVIDER_PENDING: frozenset({
        EffectState.CONFIRMED_APPLIED, EffectState.PARTIALLY_APPLIED,
        EffectState.OUTCOME_UNKNOWN,
    }),
    EffectState.OUTCOME_UNKNOWN: frozenset({
        # Evidence-based resolutions only (§5.10); an operator guess must
        # NOT convert unknown → confirmed_not_applied (§5.12).
        EffectState.CONFIRMED_APPLIED, EffectState.CONFIRMED_NOT_APPLIED,
        EffectState.PARTIALLY_APPLIED, EffectState.CLOSED_UNRESOLVED,
    }),
    EffectState.CONFIRMED_APPLIED: frozenset(),
    EffectState.CONFIRMED_NOT_APPLIED: frozenset(),
    EffectState.PARTIALLY_APPLIED: frozenset(),
    EffectState.CLOSED_UNRESOLVED: frozenset(),
    EffectState.CANCELLED_BEFORE_DISPATCH: frozenset(),
}


def effect_transition_valid(current: EffectState, target: EffectState) -> bool:
    return target in ALLOWED_EFFECT_TRANSITIONS.get(current, frozenset())


class EffectTransitionError(Exception):
    """A conditional effect resolution lost a race or attempted an invalid
    transition — rejected with a conflict (§5.11), never silently applied."""

    def __init__(self, message: str, current_state: EffectState,
                 state_version: int):
        super().__init__(message)
        self.current_state = current_state
        self.state_version = state_version


class EffectOutcome(str, Enum):
    """§5.5 — the structured result of ONE dispatch attempt. Exceptions are
    NOT uniformly 'failed': the transport outcome distinguishes what the
    evidence can and cannot prove (§5.6 crash table)."""

    CONFIRMED_APPLIED = "confirmed_applied"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"   # provider refused pre-execution
    PARTIALLY_APPLIED = "partially_applied"
    PROVIDER_PENDING = "provider_pending"
    OUTCOME_UNKNOWN = "outcome_unknown"               # timeout / lost response
    REJECTED_BEFORE_DISPATCH = "rejected_before_dispatch"  # auth/approval/cancel
    FAILED_LOCAL = "failed_local"                     # never reached the wire


@dataclass
class Effect:
    """§5.3/§5.4 — one intended external action, canonically bound BEFORE
    dispatch. Raw secrets never enter the digest or record (§5.3): the
    credential is referenced, not embedded."""

    effect_id: str
    operation_name: str                  # e.g. email.send, http.request, browser.submit
    owner_user_id: str = ""
    organization_id: Optional[str] = None
    command_id: Optional[str] = None
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    operation_instance_id: Optional[str] = None   # stable workflow occurrence
    connector_id: Optional[str] = None
    connector_revision: Optional[str] = None
    external_account_id: Optional[str] = None     # internal account reference
    credential_reference: Optional[str] = None    # NEVER a raw secret
    target: str = ""                              # remote target (address/URL/record)
    canonical_arguments: Dict[str, Any] = field(default_factory=dict)
    request_digest: str = ""                      # versioned canonicalization (§5.3)
    approval_reference: Optional[str] = None
    state: EffectState = EffectState.PREPARED
    state_version: int = 1
    provider_idempotency_key: Optional[str] = None
    compensates_effect_id: Optional[str] = None
    created_at: int = 0
    updated_at: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_EFFECT_STATES


@dataclass
class EffectReceipt:
    """§5.9 — the evidence actually available, with the distinctions that
    matter (accepted ≠ delivered; async job ≠ committed; 202 ≠ done)."""

    effect_id: str
    effect_attempt_id: str
    provider: str
    external_account_scope: Optional[str] = None
    provider_request_id: Optional[str] = None
    provider_operation_id: Optional[str] = None
    remote_resource_id: Optional[str] = None
    provider_status: str = ""            # e.g. smtp_accepted, http_202, committed
    accepted_at: Optional[int] = None
    observed_at: int = 0
    response_digest: Optional[str] = None
    evidence_reference: Optional[str] = None
    receipt_schema_version: int = 1
    detail: Dict[str, Any] = field(default_factory=dict)   # redacted summaries


@dataclass
class EffectDispatchResult:
    """§5.5 — what the effect boundary returns instead of raising everything."""

    outcome: EffectOutcome
    effect_id: str
    effect_attempt_id: Optional[str] = None
    receipt: Optional[EffectReceipt] = None
    retry_disposition: str = "none"      # none|retry_same_key|retry_items|await_approval
    reconciliation_required: bool = False
    safe_user_message: str = ""
