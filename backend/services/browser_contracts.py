"""Browser specialist session contracts (Phase 15 §15.20).

A browser delegation carries its OWN typed session contract: session
identity, controller ownership epoch, allowed origins, permitted
actions, deadline, and recovery level. Targets must be CURRENTLY
resolved (stale targets after a handoff are refused), and a click is
never proof of completion — the verified postcondition is.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class BrowserContractError(ValueError):
    pass


PERMITTED_ACTIONS = {"observe", "navigate", "click", "fill", "scroll",
                     "wait"}


@dataclass
class BrowserSessionContract:
    session_id: str
    controller_worker_id: str
    controller_epoch: str
    allowed_origins: List[str]
    permitted_actions: List[str]
    deadline_at: int
    recovery_level: str                  # from §8.19 determine_recovery_level
    run_id: str
    task_id: str
    expected_postcondition: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        problems = []
        if not self.session_id or not self.controller_worker_id or \
                not self.controller_epoch:
            problems.append("session identity/controller ownership missing")
        if not self.allowed_origins:
            problems.append("no allowed origins (scope is unbounded)")
        unknown = set(self.permitted_actions or []) - PERMITTED_ACTIONS
        if unknown:
            problems.append(f"unknown actions: {sorted(unknown)}")
        if not self.permitted_actions:
            problems.append("no permitted actions (the contract is empty)")
        if not self.deadline_at:
            problems.append("no deadline")
        if not self.recovery_level:
            problems.append("no recovery level (§8.19)")
        if problems:
            raise BrowserContractError(
                "incomplete browser session contract: " + "; ".join(problems))


def check_target_current(contract: BrowserSessionContract, *,
                         resolved_url: str,
                         last_handoff_epoch: str) -> None:
    """§15.20: stale targets after a handoff are REFUSED — the target
    must be resolved against the CURRENT controller epoch."""
    if last_handoff_epoch != contract.controller_epoch:
        raise BrowserContractError(
            "stale target: the page was resolved under a previous "
            "controller epoch — re-resolve before acting")


def completion_requires_postcondition(contract: BrowserSessionContract, *,
                                      clicked: bool,
                                      postcondition_verified: bool) -> bool:
    """§15.20 acceptance: a click is NEVER proof of task completion —
    completion is the VERIFIED POSTCONDITION (or an explicitly
    observational contract with no postcondition declared)."""
    if not contract.expected_postcondition:
        return clicked   # observational contract: actions are the work
    return postcondition_verified
