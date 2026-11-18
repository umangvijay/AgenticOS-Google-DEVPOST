"""Structured handoffs (Phase 15 §15.12) and partial completion
(§15.15) — the typed boundary between specialists.

A handoff is NOT a conversation transcript: it carries typed results,
evidence references, limitations, and unresolved questions, with every
content item CLASSIFIED by provenance. Instructions embedded inside a
retrieved document are DATA — they can never become authority for the
receiving specialist (only TrustedSystemPolicy and UserInstruction
classes carry authority, and those come from the workflow context, not
from content).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ContentClass(str, Enum):
    """§15.12: the provenance classes. Authority rules are attached to
    the CLASS, not the content: an embedded instruction inside Tool or
    PageContent is untrusted data, forever."""
    USER_INSTRUCTION = "user_instruction"         # from the user's workflow context
    TRUSTED_SYSTEM_POLICY = "trusted_system_policy"  # from deterministic services
    TOOL_OR_PAGE_CONTENT = "tool_or_page_content"    # untrusted data
    AGENT_PROPOSAL = "agent_proposal"             # a specialist's suggestion
    VERIFIED_OBSERVATION = "verified_observation"    # checked evidence
    UNVERIFIED_ASSERTION = "unverified_assertion"    # claims without evidence


# Which classes may carry AUTHORITY (bind the receiver to do something).
AUTHORITY_CARRYING = {ContentClass.USER_INSTRUCTION,
                      ContentClass.TRUSTED_SYSTEM_POLICY}


class HandoffError(ValueError):
    pass


@dataclass
class HandoffItem:
    content_class: ContentClass
    payload: Any
    authority: bool = False        # does this item BIND the receiver?

    def validate(self) -> None:
        """§15.12 acceptance: an embedded instruction inside retrieved
        content cannot become authority — only the authority-carrying
        classes may bind, and their payloads must be short, explicit
        directives (not document blobs)."""
        if self.authority and self.content_class not in AUTHORITY_CARRYING:
            raise HandoffError(
                f"content class '{self.content_class.value}' cannot carry "
                "authority — instructions embedded in tool/page content "
                "are untrusted data (§15.12)")
        if self.authority and isinstance(self.payload, str) and \
                len(self.payload) > 2000:
            raise HandoffError(
                "an authority-carrying item must be an explicit "
                "directive, not a document blob")


@dataclass
class Handoff:
    """§15.12: the typed handoff between specialists."""
    from_role: str
    to_role: str
    results: Dict[str, Any] = field(default_factory=dict)
    evidence_refs: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    unresolved_questions: List[str] = field(default_factory=list)
    items: List[HandoffItem] = field(default_factory=list)

    def validate(self) -> None:
        for item in self.items:
            item.validate()

    def authority_items(self) -> List[HandoffItem]:
        self.validate()
        return [i for i in self.items if i.authority]


# ── §15.15: partial completion ─────────────────────────────────────────

class RequirementState(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    WAITING = "waiting"
    NEEDS_REVIEW = "needs_review"
    UNCERTAIN = "uncertain"          # uncertain external effects (§5)
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


@dataclass
class Requirement:
    requirement_id: str
    description: str
    state: RequirementState
    reason: str = ""
    next_action: str = ""


def assemble_partial_result(requirements: List[Requirement]) -> Dict[str, Any]:
    """§15.15: describe EXACTLY what was delivered and what remains —
    each requirement separately stated with its reason and next action.
    Partial external effects are NEVER auto-converted into verified
    partial achievement: the states come from the evidence, not from a
    completion percentage."""
    delivered = [r for r in requirements
                 if r.state is RequirementState.VERIFIED]
    remaining = [r for r in requirements
                 if r.state is not RequirementState.VERIFIED]
    if not requirements:
        overall = "unsupported"
    elif not remaining:
        overall = "verified"
    elif delivered:
        overall = "partial"
    else:
        overall = "incomplete"
    return {
        "overall": overall,
        "delivered": [{"id": r.requirement_id,
                       "description": r.description} for r in delivered],
        "remaining": [{"id": r.requirement_id,
                       "description": r.description,
                       "state": r.state.value,
                       "reason": r.reason,
                       "next_action": r.next_action}
                      for r in remaining],
        # The honest summary the user reads — never a bare percentage.
        "summary": (
            f"{len(delivered)} of {len(requirements)} requirements "
            f"verified; {len(remaining)} remaining"
            + (" — see the per-requirement reasons and next actions"
               if remaining else "")),
    }
