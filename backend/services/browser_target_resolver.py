"""Semantic target resolver + staleness rules (Phase 9 §9.4/§9.5).

Resolution order (§9.4): role+accessible-name within a verified container →
associated label → documented stable test identifier → stable business
identity + semantic child control → validated structural locator →
controlled visual targeting. Repeated controls are SCOPED first (invoice
row INV-1042 → Delete inside that row). Ambiguity is never silently
resolved with .first/.nth(0).

Staleness (§9.5): observations are invalidated by main-document navigation,
frame navigation/detachment, page replacement, takeover, controller-
generation change, target replacement, material field changes, or
credential/account changes — NOT by unrelated animations or clock ticks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from backend.models.browser_observation import (
    BrowserObservation,
    ObservedElement,
)


class TargetResolutionFailure(str, Enum):
    """§9.5 structured failures."""
    STALE_OBSERVATION = "STALE_OBSERVATION"
    TARGET_MISSING = "TARGET_MISSING"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    TARGET_IDENTITY_CHANGED = "TARGET_IDENTITY_CHANGED"
    FRAME_REPLACED = "FRAME_REPLACED"
    ORIGIN_CHANGED = "ORIGIN_CHANGED"
    CONTROL_CHANGED = "CONTROL_CHANGED"
    SCOPE_TRUNCATED = "SCOPE_TRUNCATED"


@dataclass
class ResolvedTarget:
    """The resolved target: the observed element plus the Playwright locator
    strategy to re-resolve it against the CURRENT page (§9.4 — locators
    resolve against the live page; stale element handles are never kept)."""
    element: ObservedElement
    strategy: str                     # role_name | label | test_id | business_identity | structural
    locator: Dict[str, Any]           # e.g. {"role": "button", "name": "Delete", "container_identity": "INV-1042"}


def resolve_target(observation: BrowserObservation, *, target_ref: str = "",
                   role: Optional[str] = None, accessible_name: str = "",
                   business_identity: str = "",
                   container_identity: str = "") -> Tuple[Optional[ResolvedTarget],
                                                          Optional[TargetResolutionFailure],
                                                          str]:
    """§9.4: resolve by the documented order. Returns (target, failure, detail).
    Ambiguity is REPORTED, never silently resolved (§9.4)."""
    if observation.truncated and not observation.scopes_observed:
        # §9.3 req 6: never report "target absent" merely because it was
        # outside the observed batch — report truncation for a scoped re-run.
        if target_ref and observation.find_by_ref(target_ref) is None and \
                not observation.elements_remaining() == 0:
            return None, TargetResolutionFailure.SCOPE_TRUNCATED, (
                f"observation truncated ({observation.elements_remaining()} "
                "elements not yet observed); request a scoped observation")

    candidates: List[ObservedElement] = []
    if target_ref:
        found = observation.find_by_ref(target_ref)
        if found is None:
            return None, TargetResolutionFailure.TARGET_MISSING, \
                f"target reference {target_ref} not in observation"
        candidates = [found]
    else:
        for e in observation.elements:
            if role and e.semantic_role != role:
                continue
            if accessible_name and accessible_name.lower() not in e.accessible_name.lower():
                continue
            if business_identity and e.business_identity != business_identity:
                continue
            if container_identity and container_identity not in e.semantic_container:
                continue
            candidates.append(e)

    if not candidates:
        return None, TargetResolutionFailure.TARGET_MISSING, "no matching element"
    if len(candidates) > 1:
        return None, TargetResolutionFailure.TARGET_AMBIGUOUS, (
            f"{len(candidates)} matching elements — scope by container or "
            "business identity (§9.4: never .first/.nth(0))")

    element = candidates[0]
    # §9.4 resolution order → locator strategy.
    if element.semantic_role and element.accessible_name:
        strategy = "role_name"
        locator = {"role": element.semantic_role, "name": element.accessible_name}
    elif element.label:
        strategy = "label"
        locator = {"label": element.label}
    elif element.locator_candidates:
        test_id = next((c for c in element.locator_candidates
                        if c.get("kind") == "test_id"), None)
        if test_id:
            strategy = "test_id"
            locator = {"test_id_attribute": test_id.get("attribute", "data-testid"),
                       "value": test_id.get("value")}
        else:
            strategy = "structural"
            locator = element.locator_candidates[0]
    else:
        strategy = "structural"
        locator = {"tag": element.tag, "container": element.semantic_container}

    # Business-identity scoping for repeated controls (§9.4).
    if element.business_identity:
        locator["container_identity"] = element.business_identity

    return ResolvedTarget(element=element, strategy=strategy, locator=locator), None, ""


# ── §9.5: staleness rules ─────────────────────────────────────────────

@dataclass
class StalenessContext:
    """The context an observation was captured under; compared against the
    CURRENT context before execution (§9.5)."""
    observation_id: str
    document_generation: int
    controller_generation: int
    owner_generation: int
    page_id: str
    frame_id: str
    origin: str
    target_identity: str = ""
    material_digest: str = ""
    controller_is_human: bool = False
    credential_binding_id: Optional[str] = None


def check_staleness(observation_context: StalenessContext,
                    current: StalenessContext) -> Tuple[Optional[TargetResolutionFailure], str]:
    """§9.5: ordered invalidation checks. Unrelated animations/clock ticks do
    NOT invalidate — only RELEVANT document/target changes do."""
    if observation_context.controller_is_human or current.controller_is_human:
        return TargetResolutionFailure.CONTROL_CHANGED, \
            "human takeover occurred after observation"
    if observation_context.controller_generation != current.controller_generation:
        return TargetResolutionFailure.CONTROL_CHANGED, \
            f"controller generation changed ({observation_context.controller_generation} → {current.controller_generation})"
    if observation_context.owner_generation != current.owner_generation:
        return TargetResolutionFailure.STALE_OBSERVATION, \
            f"owner generation changed ({observation_context.owner_generation} → {current.owner_generation})"
    if observation_context.page_id != current.page_id:
        return TargetResolutionFailure.FRAME_REPLACED, "page replaced"
    if observation_context.frame_id != current.frame_id:
        return TargetResolutionFailure.FRAME_REPLACED, "frame navigated or detached"
    if observation_context.origin != current.origin:
        return TargetResolutionFailure.ORIGIN_CHANGED, \
            f"origin changed ({observation_context.origin} → {current.origin})"
    if observation_context.document_generation != current.document_generation:
        return TargetResolutionFailure.STALE_OBSERVATION, \
            f"document regenerated ({observation_context.document_generation} → {current.document_generation})"
    if observation_context.target_identity and \
            observation_context.target_identity != current.target_identity:
        return TargetResolutionFailure.TARGET_IDENTITY_CHANGED, \
            "the target's business identity changed"
    if observation_context.material_digest and \
            observation_context.material_digest != current.material_digest:
        return TargetResolutionFailure.TARGET_IDENTITY_CHANGED, \
            "material field/resource details changed since approval"
    # §10.8 test: a credential-binding change invalidates the context.
    if observation_context.credential_binding_id and \
            current.credential_binding_id and \
            observation_context.credential_binding_id != current.credential_binding_id:
        return TargetResolutionFailure.CONTROL_CHANGED, \
            "credential binding changed since observation"
    return None, ""


# ── §9.8: outcome verification levels ─────────────────────────────────

class OutcomeLevel(str, Enum):
    INTERACTION_COMPLETED = "interaction_completed"       # the click/fill happened
    APPLICATION_ACKNOWLEDGED = "application_acknowledged" # the site responded
    BUSINESS_VERIFIED = "business_verified"               # evidence confirms the outcome


class OutcomeVerdict(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    OUTCOME_UNCERTAIN = "OUTCOME_UNCERTAIN"


@dataclass
class OutcomeEvidence:
    level: OutcomeLevel
    detail: str = ""
    resource_identity: Optional[str] = None   # e.g. created record id
    read_back: Optional[Dict[str, Any]] = None


def verify_outcome(action_kind: str, evidence: Optional[OutcomeEvidence],
                   model_claimed_success: bool = False,
                   executed_steps_all_errored: bool = False) -> Tuple[OutcomeVerdict, str]:
    """§9.8: separate interaction-completed from application-acknowledged
    from business-verified. The model saying 'done' is NOT independent
    verification. An INCONCLUSIVE observation after a potentially submitted
    write must NOT trigger automatic resubmission (§9.8)."""
    if evidence is None or evidence.level is None:
        if model_claimed_success:
            return (OutcomeVerdict.INCONCLUSIVE,
                    "model claimed success but no independent evidence was recorded")
        return OutcomeVerdict.FAILED, "no outcome evidence"
    if evidence.level is OutcomeLevel.BUSINESS_VERIFIED:
        return OutcomeVerdict.VERIFIED, evidence.detail
    if evidence.level is OutcomeLevel.APPLICATION_ACKNOWLEDGED:
        # Acknowledged (e.g. toast per site profile) — but not full read-back.
        return OutcomeVerdict.VERIFIED, f"acknowledged: {evidence.detail}"
    # §10.8 fix: a GENERIC click with only "interaction completed" evidence
    # is NOT verified — a click can delete, publish, or submit; its business
    # effect cannot be inferred safely from the interaction name alone. Only
    # explicit reads (navigate) may use interaction evidence alone.
    if evidence.level is OutcomeLevel.INTERACTION_COMPLETED:
        if action_kind in ("navigate", "observe"):
            return OutcomeVerdict.VERIFIED, f"interaction completed: {evidence.detail}"
        return (OutcomeVerdict.OUTCOME_UNCERTAIN,
                "interaction completed but the business outcome was not verified "
                "— reconcile before retrying (§9.8/§10.8)")
    if evidence.level is OutcomeLevel.APPLICATION_ACKNOWLEDGED:
        # §10.8: acknowledged (e.g. toast per site profile) is evidence the
        # site responded, but the business outcome needs profile-specific
        # confirmation. Report as uncertain for writes, verified for reads.
        if action_kind in ("navigate", "observe"):
            return OutcomeVerdict.VERIFIED, f"acknowledged: {evidence.detail}"
        return (OutcomeVerdict.OUTCOME_UNCERTAIN,
                "the application responded but the business outcome was not "
                "read back — reconcile before retrying (§10.8)")
    return (OutcomeVerdict.OUTCOME_UNCERTAIN,
            "insufficient evidence for the business outcome")
