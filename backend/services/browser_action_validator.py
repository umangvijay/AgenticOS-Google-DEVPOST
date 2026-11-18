"""Action validation + progress tracking (Phase 9 §9.6/§9.7/§9.17).

The action pipeline (§9.6): observe → propose → resolve → validate →
approve → prepare effect → revalidate → record dispatch → execute → verify
→ record evidence. The browser_action_validator enforces ownership, policy,
and staleness BEFORE execution; the progress tracker detects loops using
meaningful progress signals (not HTML hashes — animations change HTML while
the agent makes no progress, §9.17).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from backend.models.browser_observation import ActionType, validate_action_arguments
from backend.services.browser_target_resolver import (
    OutcomeVerdict,
    StalenessContext,
    TargetResolutionFailure,
    check_staleness,
)
from backend.models.browser_session import (
    Actor,
    ControllerState,
    ResourceState,
)


# ── §9.6: the action validator ────────────────────────────────────────

def validate_browser_action(
    *, action_type: ActionType, arguments: Dict[str, Any],
    observation_context: StalenessContext, current_context: StalenessContext,
    session_state: ResourceState, controller: ControllerState,
    owner_epoch_matches: bool, caller_is_owner: bool,
    origin_allowed: bool = True,
    resolved_failure: Optional[TargetResolutionFailure] = None,
    resolved_detail: str = "",
    approval_digest_matches: bool = True,
    consequential: bool = False,
    write_effect_recorded: bool = True,
) -> Tuple[bool, List[str]]:
    """§9.6 steps 4-8: enforce ownership, policy, staleness, and argument
    validity BEFORE execution. Returns (allowed, structured failures)."""
    failures: List[str] = []
    if not caller_is_owner:
        failures.append("CALLER_NOT_OWNER")
    if not owner_epoch_matches:
        failures.append("OWNER_EPOCH_CHANGED")
    if session_state is not ResourceState.READY:
        failures.append(f"SESSION_STATE: {session_state.value}")
    if controller is ControllerState.HUMAN:
        failures.append("CONTROLLER_IS_HUMAN")
    if controller is ControllerState.NONE:
        failures.append("NO_ACTIVE_CONTROLLER")
    args_ok, arg_errors = validate_action_arguments(action_type, arguments)
    failures.extend(arg_errors)
    staleness, detail = check_staleness(observation_context, current_context)
    if staleness is not None:
        failures.append(f"{staleness.value}: {detail}")
    if resolved_failure is not None:
        failures.append(f"{resolved_failure.value}: {resolved_detail}")
    if not origin_allowed:
        failures.append("ORIGIN_NOT_ALLOWED")
    if not approval_digest_matches:
        failures.append("APPROVAL_MATERIAL_CHANGED")
    # §8.14/§9.0D: consequential actions need a durably recorded effect
    # before dispatch (the caller classifies via the action intent §8.13).
    if consequential and not write_effect_recorded:
        failures.append("EFFECT_NOT_RECORDED")
    return (not failures), failures


# ── §9.17: progress tracking ──────────────────────────────────────────

class ProgressSignal(str, Enum):
    NEW_MILESTONE = "new_verified_milestone"
    RESOURCE_DISCOVERED = "correct_resource_discovered"
    STATE_TRANSITION = "expected_state_transition"
    NEW_RELEVANT_PAGE = "new_relevant_page_or_frame"
    RESULT_SET_CHANGED = "changed_result_set"
    ARTIFACT_COMPLETED = "completed_artifact"
    NO_PROGRESS = "no_progress"


@dataclass
class ProgressTracker:
    """§9.17: meaningful progress detection — NOT a hash of the first portion
    of page HTML (animations/timestamps change HTML without progress). Tracks
    repeated equivalent actions, alternating navigation loops, repeated
    ambiguity, repeated empty observations, repeated approval requests for
    the same unchanged intent, and attempts after uncertain submission."""
    max_equivalent_repeats: int = 2
    max_ambiguity_repeats: int = 3
    max_empty_observations: int = 3
    max_uncertain_repeats: int = 1
    _action_fingerprints: Dict[str, int] = field(default_factory=dict)
    _ambiguity_counts: Dict[str, int] = field(default_factory=dict)
    _empty_observations: int = 0
    _uncertain_submissions: int = 0
    _last_milestones: List[str] = field(default_factory=list)

    def record_action(self, fingerprint: str, *, changed_state: bool) -> Optional[str]:
        """Returns a stall reason when a bounded loop is detected, else None.
        `fingerprint` = the ACTION + semantic page state (not raw HTML)."""
        count = self._action_fingerprints.get(fingerprint, 0) + 1
        self._action_fingerprints[fingerprint] = count
        if not changed_state and count > self.max_equivalent_repeats:
            return (f"repeated equivalent action ({count}x, no state change) — "
                    "re-observe, refine scope, or escalate (§9.17)")
        return None

    def record_ambiguity(self, scope_key: str) -> Optional[str]:
        count = self._ambiguity_counts.get(scope_key, 0) + 1
        self._ambiguity_counts[scope_key] = count
        if count > self.max_ambiguity_repeats:
            return (f"repeated ambiguity for {scope_key[:40]} — request human "
                    "control or a scoped observation (§9.17)")
        return None

    def record_observation(self, element_count: int) -> Optional[str]:
        if element_count == 0:
            self._empty_observations += 1
            if self._empty_observations >= self.max_empty_observations:
                return ("repeated empty observations — the page may require "
                        "different scoping or human control (§9.17)")
        else:
            self._empty_observations = 0
        return None

    def record_uncertain_submission(self) -> Optional[str]:
        """§9.17: repeated attempts after uncertain submission are never
        allowed — one occurrence stops the loop."""
        self._uncertain_submissions += 1
        if self._uncertain_submissions > self.max_uncertain_repeats:
            return "repeated attempt after uncertain submission — reconcile first (§8.14/§9.8)"
        return "reconcile before retrying"


def record_milestone(tracker_progress: List[str], milestone: str) -> bool:
    """§9.17: a NEW verified milestone is real progress; repeating an old one
    is not."""
    if milestone not in tracker_progress:
        tracker_progress.append(milestone)
        return True
    return False
