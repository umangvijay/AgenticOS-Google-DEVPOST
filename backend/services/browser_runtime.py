"""Browser runtime interface (Phase 8 §8.8) + action intents (§8.13) +
network policy (§8.11) + credential injection authorization (§8.12) +
takeover grants (§8.16) + recovery levels (§8.19).

The runtime is the ONE controlled browser execution path: every operation
verifies the ownership epoch, the current controller, session expiry, the
policy, and the relevant action authorization. The orchestrator cannot
launch an unrestricted browser through a separate convenience path (§8.8
acceptance).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now
from backend.models.browser_session import (
    Actor,
    ChallengeState,
    ControllerState,
    ResourceState,
    SessionTransitionError,
)


# ── §8.13: structured action intents ──────────────────────────────────

class ActionKind(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SUBMIT = "submit"
    DELETE = "delete"           # non-form destructive control
    PUBLISH = "publish"         # JS-implemented publish
    SETTINGS_CHANGE = "settings_change"
    AUTOSAVE = "autosave"
    SELECT_TRANSACTION = "select_transaction"
    OBSERVE = "observe"


# §8.13: consequential action kinds — a click is consequential even outside
# a form when its kind is destructive/publishing/settings/transaction.
CONSEQUENTIAL_KINDS = {
    ActionKind.SUBMIT, ActionKind.DELETE, ActionKind.PUBLISH,
    ActionKind.SETTINGS_CHANGE, ActionKind.AUTOSAVE,
    ActionKind.SELECT_TRANSACTION,
}


@dataclass
class ActionIntent:
    """§8.13 — the material action identity. Approval binds to THIS (target,
    action, meaningful payload, session identity), not a descriptive
    sentence (§8.4 D)."""
    action_id: str
    session_id: str
    controller_epoch: str
    page_id: Optional[str]
    frame_origin: str
    action_kind: ActionKind
    target_identity: str
    material_payload: Dict[str, Any] = field(default_factory=dict)
    expected_effect: str = ""
    expected_postcondition: str = ""
    risk_classification: str = "medium"

    def material_digest(self) -> str:
        """§8.13 acceptance: changing a recipient, amount, resource, or
        submitted message changes THIS digest → the prior approval is
        invalidated when the detail is material. Sensitive payloads are
        digested, not logged (§8.13).

        9.0D: the digest deliberately EXCLUDES the controller epoch and
        owner generation — a business-action identity must stay STABLE
        across ownership changes and recovery, so a recovery attempt keeps
        the same effect and a new ownership cannot launder an old action."""
        material = json.dumps({
            "session": self.session_id,
            "origin": self.frame_origin, "kind": self.action_kind.value,
            "target": self.target_identity,
            "payload": self.material_payload,
        }, sort_keys=True, default=str)
        return hashlib.sha256(material.encode()).hexdigest()


def build_action_intent(*, session_id: str, controller_epoch: str,
                        frame_origin: str, action_kind: ActionKind,
                        target_identity: str,
                        material_payload: Optional[Dict[str, Any]] = None,
                        expected_effect: str = "",
                        expected_postcondition: str = "",
                        risk_classification: str = "medium",
                        page_id: Optional[str] = None) -> ActionIntent:
    return ActionIntent(
        action_id=f"act_{uuid.uuid4().hex[:20]}",
        session_id=session_id, controller_epoch=controller_epoch,
        page_id=page_id, frame_origin=frame_origin, action_kind=action_kind,
        target_identity=target_identity,
        material_payload=material_payload or {},
        expected_effect=expected_effect,
        expected_postcondition=expected_postcondition,
        risk_classification=risk_classification)


# ── §8.11: network policy (navigation vs resource categories) ─────────

@dataclass
class BrowserNetworkPolicy:
    """§8.11: separate destination categories — a trusted navigation origin
    does NOT authorize sibling hosts, alternate ports, or third-party
    resources. Enforcement is backed by runtime egress controls; the
    in-process hook is not the whole boundary."""
    policy_id: str
    navigation_origins: List[str] = field(default_factory=list)
    identity_provider_origins: List[str] = field(default_factory=list)
    resource_origins: List[str] = field(default_factory=list)
    api_origins: List[str] = field(default_factory=list)
    websocket_origins: List[str] = field(default_factory=list)

    @staticmethod
    def normalize_origin(url_or_origin: str) -> str:
        """scheme + hostname + effective port (§8.11)."""
        from urllib.parse import urlparse
        parsed = urlparse(url_or_origin if "//" in url_or_origin else f"https://{url_or_origin}")
        scheme = parsed.scheme or "https"
        host = parsed.hostname or ""
        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80 if scheme == "http" else None
        return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"

    def _allowed(self, origin: str, categories: List[List[str]]) -> bool:
        normalized = self.normalize_origin(origin)
        for category in categories:
            for allowed in category:
                if normalized == self.normalize_origin(allowed):
                    return True
        return False

    def navigation_allowed(self, origin: str) -> bool:
        return self._allowed(origin, [self.navigation_origins,
                                      self.identity_provider_origins])

    def resource_allowed(self, origin: str) -> bool:
        """§8.11 acceptance: a permitted page cannot send data to an
        unapproved destination just because the request is not a navigation
        — fetch/XHR/images/scripts follow the RESOURCE policy."""
        return self._allowed(origin, [self.resource_origins,
                                      self.api_origins,
                                      self.navigation_origins])

    def websocket_allowed(self, origin: str) -> bool:
        return self._allowed(origin, [self.websocket_origins])


# ── §8.12: credential injection authorization ─────────────────────────

class CredentialInjectionDecision(str, Enum):
    ALLOWED = "allowed"
    DENIED_NOT_OWNER = "denied_not_owner"
    DENIED_ORIGIN = "denied_origin"
    DENIED_FIELD_PURPOSE = "denied_field_purpose"
    DENIED_HUMAN_CONTROL = "denied_human_control"
    DENIED_CHALLENGE_FIELD = "denied_challenge_field"
    DENIED_REVOKED = "denied_revoked"
    DENIED_STALE_OBSERVATION = "denied_stale_observation"


def evaluate_credential_injection(
    *, owner_user_id: str, requesting_user_id: str,
    binding_status: str, binding_owner: str,
    expected_top_level_origin: str, current_top_level_origin: str,
    expected_frame_origin: str, current_frame_origin: str,
    field_purpose: str, observed_field_purpose: str,
    controller: ControllerState,
    challenge: ChallengeState,
    observation_version: int, current_observation_version: int,
) -> tuple[CredentialInjectionDecision, str]:
    """§8.12: the injector verifies ownership, page/frame origins, field
    purpose, agent control, observation validity, human-only challenge
    fields, and credential revocation BEFORE injecting.

    Acceptance (§8.12): a page instructing the agent to place
    `{{secret:password}}` in a support-message field is DENIED — the field
    purpose must match the authorized purpose."""
    if owner_user_id != requesting_user_id or binding_owner != requesting_user_id:
        return CredentialInjectionDecision.DENIED_NOT_OWNER, \
            "Credential binding is not owned by the requester"
    if binding_status != "active":
        return CredentialInjectionDecision.DENIED_REVOKED, "Credential is revoked"
    if expected_top_level_origin != current_top_level_origin or \
            expected_frame_origin != current_frame_origin:
        return CredentialInjectionDecision.DENIED_ORIGIN, \
            "Current page/frame origin does not match the authorized origins"
    if field_purpose != observed_field_purpose:
        return (CredentialInjectionDecision.DENIED_FIELD_PURPOSE,
                f"Field purpose mismatch: authorized {field_purpose!r}, "
                f"target is {observed_field_purpose!r}")
    # §8.12: the challenge-field check is MORE SPECIFIC than the controller
    # check — an OTP/MFA field is human-only regardless of controller.
    if challenge in (ChallengeState.OTP, ChallengeState.MFA, ChallengeState.REAUTH_REQUIRED):
        return CredentialInjectionDecision.DENIED_CHALLENGE_FIELD, \
            "OTP/MFA/re-auth fields are human-only (§8.12)"
    if controller is not ControllerState.AGENT:
        return CredentialInjectionDecision.DENIED_HUMAN_CONTROL, \
            "Credential injection requires agent control"
    if observation_version != current_observation_version:
        return CredentialInjectionDecision.DENIED_STALE_OBSERVATION, \
            "The observation is stale; re-observe before injecting"
    return CredentialInjectionDecision.ALLOWED, ""


# ── §8.16: short-lived, revocable takeover grants ─────────────────────

@dataclass
class TakeoverGrant:
    """§8.16: view ≠ control. Permission to view a screenshot is NOT
    permission to control the browser. Short-lived, revocable, bound to the
    session + controller epoch + user."""
    grant_id: str
    session_id: str
    user_id: str
    tenant_id: str
    controller_epoch: str
    permissions: List[str] = field(default_factory=lambda: ["view"])
    expires_at: int = 0
    used_at: Optional[int] = None
    revoked_at: Optional[int] = None

    def live_for(self, user_id: str, session_id: str, now: int, *,
                 tenant_id: Optional[str] = None,
                 controller_epoch: Optional[str] = None) -> bool:
        """§8.16/9.0A: STRICT expiry (now < expires_at — a grant is dead at
        its exact expiration timestamp); the caller's tenant and controller
        epoch must match the grant's recorded binding."""
        if now >= self.expires_at:
            return False
        if tenant_id is not None and self.tenant_id != tenant_id:
            return False
        if controller_epoch is not None and self.controller_epoch != controller_epoch:
            return False
        return (self.user_id == user_id and self.session_id == session_id
                and self.revoked_at is None)

    def can_control(self) -> bool:
        return "control" in self.permissions or "complete_challenge" in self.permissions


def issue_takeover_grant(*, session_id: str, user_id: str, tenant_id: str,
                         controller_epoch: str, ttl_seconds: int = 300,
                         permissions: Optional[List[str]] = None) -> TakeoverGrant:
    """§8.16 lifecycle steps 1-5: ownership verified by the caller, agent
    paused, epoch transitioned, then the short-lived grant is issued."""
    return TakeoverGrant(
        grant_id=f"grant_{uuid.uuid4().hex[:20]}",
        session_id=session_id, user_id=user_id, tenant_id=tenant_id,
        controller_epoch=controller_epoch,
        permissions=permissions or ["view", "complete_challenge"],
        expires_at=epoch_now() + max(30, ttl_seconds))


def revoke_takeover_grant(grant: TakeoverGrant) -> None:
    """§8.16 step 8: revoked before control returns to the agent; revocation
    closes active control immediately."""
    grant.revoked_at = epoch_now()


# ── §8.19: recovery levels + checkpoints ──────────────────────────────

class RecoveryLevel(str, Enum):
    LIVE_CONTINUATION = "live_continuation"
    AUTHENTICATED_RECONSTRUCTION = "authenticated_reconstruction"
    REAUTH_REQUIRED = "reauthentication_required"
    MANUAL_RECONCILIATION_REQUIRED = "manual_reconciliation_required"
    UNSUPPORTED = "unsupported_recovery"


@dataclass
class BrowserCheckpoint:
    """§8.19: an encrypted checkpoint identifies context — it does NOT
    persist raw secret dictionaries (credential references only, §8.19)."""
    checkpoint_id: str
    session_id: str
    tenant_id: str
    browser_runtime_compatibility: str
    credential_binding_id: Optional[str]
    allowed_origins: List[str]
    checkpoint_version: str = "p8.v1"
    created_at: int = 0
    expires_at: int = 0
    last_verified_milestone: str = ""
    pending_effect_ids: List[str] = field(default_factory=list)
    storage_state_reference: Optional[str] = None   # protected storage ref

    def content_digest(self) -> str:
        material = json.dumps({
            "session": self.session_id, "tenant": self.tenant_id,
            "runtime": self.browser_runtime_compatibility,
            "binding": self.credential_binding_id,
            "milestone": self.last_verified_milestone,
            "version": self.checkpoint_version,
        }, sort_keys=True)
        return hashlib.sha256(material.encode()).hexdigest()


def determine_recovery_level(*, live_session_exists: bool,
                             storage_state_available: bool,
                             storage_state_valid: bool,
                             pending_uncertain_effects: bool,
                             reauth_verifiable: bool) -> RecoveryLevel:
    """§8.19: the recovery level is determined HONESTLY — saved browser
    storage state is not a complete snapshot of a running browser; uncertain
    effects must be reconciled before automatic continuation."""
    if pending_uncertain_effects:
        return RecoveryLevel.MANUAL_RECONCILIATION_REQUIRED
    if live_session_exists:
        return RecoveryLevel.LIVE_CONTINUATION
    if storage_state_available and storage_state_valid:
        return RecoveryLevel.AUTHENTICATED_RECONSTRUCTION
    if reauth_verifiable:
        return RecoveryLevel.REAUTH_REQUIRED
    return RecoveryLevel.UNSUPPORTED
