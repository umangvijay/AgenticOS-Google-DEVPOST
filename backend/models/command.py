"""Command lifecycle domain model (Phase 4, §4.2/§4.3).

The identities and the ONE lifecycle that make "accepted means durable"
enforceable. Framework-free (no FastAPI/pydantic) like the other domain
models. Spec: docs/command-lifecycle.md.

Identity separations (§4.2): command_id (one accepted instruction, stable
across request retries and restarts) is NOT run_id (one workflow execution)
is NOT task_id (logical node, stable across attempts) is NOT attempt_id (one
execution attempt). A transport redelivery is never a new attempt.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


def new_command_id() -> str:
    return "cmd_" + uuid.uuid4().hex[:24]


def new_event_id() -> str:
    return "evt_" + uuid.uuid4().hex[:24]


def new_wait_id() -> str:
    return "wait_" + uuid.uuid4().hex[:20]


class CommandState(str, Enum):
    """§4.3 — the small command lifecycle. WAITING reasons are a separate
    field, not states; transport publishing is NOT a business state (it lives
    on the outbox record)."""

    ACCEPTED = "ACCEPTED"                  # command + dispatch intent committed
    QUEUED = "QUEUED"                      # eligible work awaits execution
    RUNNING = "RUNNING"                    # a current attempt is executing
    WAITING = "WAITING"                    # depends on child/human/retry/capacity
    CANCEL_REQUESTED = "CANCEL_REQUESTED"  # cancellation intent recorded
    SUCCEEDED = "SUCCEEDED"                # work + result publication complete
    FAILED = "FAILED"                      # no further automatic progress
    CANCELLED = "CANCELLED"                # cancellation handling finished
    EXPIRED = "EXPIRED"                    # allowed lifetime ended


class WaitingReason(str, Enum):
    CAPACITY = "capacity"
    RETRY_BACKOFF = "retry_backoff"
    CHILD_BUILD = "child_build"
    HUMAN_APPROVAL = "human_approval"
    BROWSER_HANDOFF = "browser_handoff"
    BUDGET = "budget"
    AUTHORIZATION_REVIEW = "authorization_review"
    OUTCOME_RECONCILIATION = "outcome_reconciliation"


class CommandErrorCode(str, Enum):
    """§4.19 structured error codes."""

    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    QUEUE_FULL = "QUEUE_FULL"
    ADMISSION_UNAVAILABLE = "ADMISSION_UNAVAILABLE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    ACCOUNTING_UNAVAILABLE = "ACCOUNTING_UNAVAILABLE"
    AUTHORIZATION_REVOKED = "AUTHORIZATION_REVOKED"
    COMMAND_EXPIRED = "COMMAND_EXPIRED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    NOT_FOUND = "NOT_FOUND"


# ── Transition rules (§4.3) ────────────────────────────────────────────
# Every transition must state the allowed source states. A transition whose
# current state is not listed FAILS (e.g. RUNNING → SUCCEEDED must fail if
# cancellation or a newer attempt changed the state first).

ALLOWED_TRANSITIONS: Dict[CommandState, frozenset] = {
    CommandState.ACCEPTED: frozenset({
        CommandState.QUEUED, CommandState.RUNNING, CommandState.CANCEL_REQUESTED,
        CommandState.FAILED, CommandState.CANCELLED, CommandState.EXPIRED,
    }),
    CommandState.QUEUED: frozenset({
        CommandState.RUNNING, CommandState.WAITING, CommandState.CANCEL_REQUESTED,
        CommandState.FAILED, CommandState.CANCELLED, CommandState.EXPIRED,
    }),
    CommandState.RUNNING: frozenset({
        CommandState.SUCCEEDED, CommandState.FAILED, CommandState.WAITING,
        CommandState.CANCEL_REQUESTED, CommandState.CANCELLED,
        CommandState.EXPIRED,   # §4.3: the allowed lifetime can end mid-run
    }),
    CommandState.WAITING: frozenset({
        CommandState.QUEUED, CommandState.RUNNING, CommandState.CANCEL_REQUESTED,
        CommandState.FAILED, CommandState.CANCELLED, CommandState.EXPIRED,
    }),
    CommandState.CANCEL_REQUESTED: frozenset({
        CommandState.CANCELLED, CommandState.FAILED, CommandState.SUCCEEDED,
        # A completion that committed before cancellation took effect is
        # returned honestly (§4.15 race table) — SUCCEEDED stays reachable.
    }),
    # Terminal states have no outgoing transitions.
    CommandState.SUCCEEDED: frozenset(),
    CommandState.FAILED: frozenset(),
    CommandState.CANCELLED: frozenset(),
    CommandState.EXPIRED: frozenset(),
}

TERMINAL_STATES = frozenset({
    CommandState.SUCCEEDED, CommandState.FAILED,
    CommandState.CANCELLED, CommandState.EXPIRED,
})


def transition_is_valid(current: CommandState, target: CommandState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


class CommandTransitionError(Exception):
    """A lifecycle-sensitive transition was attempted from a state that does
    not allow it (§4.3) — e.g. a stale worker completing a cancelled
    command. Carries the current durable state and version."""

    def __init__(self, message: str, current_state: CommandState,
                 state_version: int, error_code: CommandErrorCode =
                 CommandErrorCode.INVALID_STATE_TRANSITION):
        super().__init__(message)
        self.current_state = current_state
        self.state_version = state_version
        self.error_code = error_code


@dataclass
class Command:
    """§4.2 — one accepted instruction. Credentials are REFERENCED by
    authorized credential identity (credential_ref), never embedded."""

    command_id: str
    command_type: str                     # workflow|build|webhook|schedule|retry|cancel
    schema_version: int = 1
    owner_user_id: str = ""
    organization_id: Optional[str] = None
    actor_type: str = "user"              # user|system|agent|webhook|scheduler
    source: str = ""                      # api|orchestrator|scheduler|webhook|recovery
    idempotency_key: Optional[str] = None
    idempotency_scoped: Optional[str] = None
    request_digest: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    run_id: Optional[str] = None
    build_id: Optional[str] = None
    parent_command_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    state: CommandState = CommandState.ACCEPTED
    state_version: int = 1
    waiting_reason: Optional[WaitingReason] = None
    deadline_at: Optional[int] = None
    cancel_requested_at: Optional[int] = None
    cancel_requested_by: Optional[str] = None
    cancel_reason: Optional[str] = None
    cancel_acknowledged_at: Optional[int] = None
    terminal_reason: Optional[str] = None
    result_reference: Optional[str] = None
    created_at: int = 0
    updated_at: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_row(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": self.command_type,
            "schema_version": self.schema_version,
            "owner_user_id": self.owner_user_id,
            "organization_id": self.organization_id,
            "actor_type": self.actor_type,
            "source": self.source,
            "idempotency_key": self.idempotency_key,
            "idempotency_scoped": self.idempotency_scoped,
            "request_digest": self.request_digest,
            "payload_json": self.payload,
            "run_id": self.run_id,
            "build_id": self.build_id,
            "parent_command_id": self.parent_command_id,
            "parent_run_id": self.parent_run_id,
            "parent_task_id": self.parent_task_id,
            "state": self.state.value,
            "state_version": self.state_version,
            "waiting_reason": self.waiting_reason.value if self.waiting_reason else None,
            "deadline_at": self.deadline_at,
            "cancel_requested_at": self.cancel_requested_at,
            "cancel_requested_by": self.cancel_requested_by,
            "cancel_reason": self.cancel_reason,
            "cancel_acknowledged_at": self.cancel_acknowledged_at,
            "terminal_reason": self.terminal_reason,
            "result_reference": self.result_reference,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def scoped_idempotency_key(owner_user_id: str, command_type: str,
                           raw_key: Optional[str]) -> Optional[str]:
    """§4.4 idempotency contract: the scope is (tenant, operation, key). The
    same raw key from another tenant or another operation is an independent
    command."""
    if not raw_key:
        return None
    return f"{owner_user_id}|{command_type}|{raw_key}"


@dataclass
class CommandReceipt:
    """§4.19 — the stable acceptance receipt returned to clients."""

    command_id: str
    run_id: Optional[str]
    state: CommandState
    accepted: bool
    deduplicated: bool
    state_version: int
    status_url: str
    error_code: Optional[CommandErrorCode] = None
    message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "command_id": self.command_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "accepted": self.accepted,
            "deduplicated": self.deduplicated,
            "state_version": self.state_version,
            "status_url": self.status_url,
        }
        if self.error_code is not None:
            out["code"] = self.error_code.value
        if self.message is not None:
            out["message"] = self.message
        return out


@dataclass
class OutboxEvent:
    """§4.8 — publication intent committed WITH the state change; the
    network publish happens afterward and is recoverable."""

    event_id: str
    command_id: Optional[str]
    aggregate_id: str
    aggregate_version: int
    event_type: str
    schema_version: int = 1
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: int = 0
    available_at: int = 0
    publish_attempts: int = 0
    dispatcher_owner: Optional[str] = None
    dispatcher_lease_expires_at: Optional[int] = None
    published_at: Optional[int] = None
    last_error_code: Optional[str] = None
