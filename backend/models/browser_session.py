"""Browser session model (Phase 8 §8.5/§8.6).

TWO layers: durable session METADATA (persisted, survives API/worker
boundaries) and LOCAL RUNTIME HANDLES (Playwright objects, process-local,
keyed by the durable session identity + ownership epoch).

§8.6: lifecycle, controller, challenge, and cleanup are SEPARATE
dimensions — resource=READY/controller=HUMAN/challenge=MFA means the
browser stays alive while the user authenticates, which is different from
resource=FAILED/controller=NONE. Permitted transitions and their actors are
defined below.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


def new_browser_session_id() -> str:
    return f"bs_{uuid.uuid4().hex[:24]}"


class ResourceState(str, Enum):
    ALLOCATING = "ALLOCATING"
    READY = "READY"
    RECOVERING = "RECOVERING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class ControllerState(str, Enum):
    AGENT = "AGENT"
    HUMAN = "HUMAN"
    NONE = "NONE"


class ChallengeState(str, Enum):
    NONE = "NONE"
    CAPTCHA = "CAPTCHA"
    OTP = "OTP"
    MFA = "MFA"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"


class CleanupState(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class Actor(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"


# §8.6 permitted transitions: (from, to, allowed_actors). A transition by a
# different actor or from an unexpected state is rejected.
ALLOWED_SESSION_TRANSITIONS: Dict[ResourceState, Dict[ResourceState, set]] = {
    ResourceState.ALLOCATING: {
        ResourceState.READY: {Actor.AGENT, Actor.SYSTEM},
        ResourceState.FAILED: {Actor.SYSTEM, Actor.AGENT},
        ResourceState.CLOSING: {Actor.SYSTEM, Actor.AGENT, Actor.HUMAN},
    },
    ResourceState.READY: {
        ResourceState.RECOVERING: {Actor.SYSTEM},
        ResourceState.CLOSING: {Actor.SYSTEM, Actor.AGENT, Actor.HUMAN},
        ResourceState.FAILED: {Actor.SYSTEM},
    },
    ResourceState.RECOVERING: {
        ResourceState.READY: {Actor.SYSTEM, Actor.AGENT},
        ResourceState.CLOSING: {Actor.SYSTEM, Actor.AGENT, Actor.HUMAN},
        ResourceState.FAILED: {Actor.SYSTEM},
    },
    ResourceState.CLOSING: {
        ResourceState.CLOSED: {Actor.SYSTEM},
        ResourceState.FAILED: {Actor.SYSTEM},
    },
    ResourceState.FAILED: {
        ResourceState.CLOSING: {Actor.SYSTEM},
    },
    ResourceState.CLOSED: {},
}


def session_transition_valid(current: ResourceState, target: ResourceState,
                             actor: Actor) -> bool:
    allowed = ALLOWED_SESSION_TRANSITIONS.get(current, {})
    return actor in allowed.get(target, set())


def ownership_epoch(tenant_id: str, session_id: str, worker_id: str,
                    attempt: int) -> str:
    """§8.7: the ownership epoch — a stable identity for (session, current
    owner). Every runtime command carries it; the runtime rejects commands
    from an obsolete epoch."""
    raw = f"{session_id}|{worker_id}|{attempt}"
    return hashlib.sha256(f"{tenant_id}|{raw}".encode()).hexdigest()[:24]


@dataclass
class BrowserSession:
    """§8.5 durable metadata — NO Playwright objects, NO secrets. Local
    runtime handles live in the process-local registry keyed by (session_id,
    owner_epoch)."""
    session_id: str
    tenant_id: str
    user_id: str
    workflow_id: Optional[str] = None
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    owner_worker_id: str = ""
    owner_epoch: str = ""
    owner_generation: int = 0
    lease_expires_at: int = 0
    runtime_session_id: Optional[str] = None
    policy_revision: Optional[str] = None
    resource_state: ResourceState = ResourceState.ALLOCATING
    controller_state: ControllerState = ControllerState.NONE
    controller_generation: int = 0
    challenge_state: ChallengeState = ChallengeState.NONE
    cleanup_state: CleanupState = CleanupState.NOT_REQUIRED
    allowed_origins: List[str] = field(default_factory=list)
    network_policy_id: Optional[str] = None
    credential_binding_id: Optional[str] = None
    checkpoint_reference: Optional[str] = None
    last_action_id: Optional[str] = None
    # §8.10: separate timers
    created_at: int = 0
    last_activity_at: int = 0
    idle_expires_at: int = 0
    handoff_expires_at: int = 0
    absolute_expires_at: int = 0

    def to_row(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id, "tenant_id": self.tenant_id,
            "user_id": self.user_id, "workflow_id": self.workflow_id,
            "run_id": self.run_id, "task_id": self.task_id,
            "owner_worker_id": self.owner_worker_id,
            "owner_epoch": self.owner_epoch,
            "owner_generation": self.owner_generation,
            "lease_expires_at": self.lease_expires_at,
            "runtime_session_id": self.runtime_session_id,
            "policy_revision": self.policy_revision,
            "resource_state": self.resource_state.value,
            "controller_state": self.controller_state.value,
            "controller_generation": self.controller_generation,
            "challenge_state": self.challenge_state.value,
            "cleanup_state": self.cleanup_state.value,
            "allowed_origins_json": self.allowed_origins,
            "network_policy_id": self.network_policy_id,
            "credential_binding_id": self.credential_binding_id,
            "checkpoint_reference": self.checkpoint_reference,
            "last_action_id": self.last_action_id,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "idle_expires_at": self.idle_expires_at,
            "handoff_expires_at": self.handoff_expires_at,
            "absolute_expires_at": self.absolute_expires_at,
        }


class SessionTransitionError(Exception):
    """A lifecycle transition attempted by the wrong actor or from an
    unexpected state (§8.6)."""

    def __init__(self, message: str, current: ResourceState):
        super().__init__(message)
        self.current = current
