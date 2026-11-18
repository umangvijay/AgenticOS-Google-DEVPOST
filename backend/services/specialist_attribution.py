"""Specialist attribution (Phase 15 §15.22) — delegation records connect
to Phase 14's decision and reward events so outcomes attach to the RIGHT
specialist and ATTEMPT, never to the whole workflow.

Recorded per attempt: which specialist was selected, which version ran,
which attempts failed, whether recovery changed the outcome, what the
verifier established, and which downstream result depended on the output.

§15.22 acceptance:
- A FAILED builder does not receive positive credit because a human
  later supplied a working connector — the human-supplied resolution
  credits the RESOLUTION (outside the bandit), not the failed attempt.
- A successful independent branch is not blamed for another branch's
  failure — credit is per attempt identity, not per workflow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AttemptRecord:
    attempt_id: str
    specialist_role: str
    specialist_version: str
    decision_id: Optional[str]      # the Phase 14 routing decision
    outcome_class: str              # verified | partial | uncertain | rejected
    evaluation_id: Optional[str] = None
    recovery_changed_outcome: bool = False
    depended_on_by: List[str] = field(default_factory=list)


def credit_attempt(attempt: AttemptRecord, *, reward_value: float) -> Dict[str, Any]:
    """Attach the reward to THIS attempt's identity (via the Phase 14
    reward-event contract). A workflow's overall success never credits
    every agent; a branch failure never blames unrelated agents."""
    if attempt.outcome_class in ("verified", "partial"):
        credit = reward_value
    elif attempt.outcome_class == "rejected":
        credit = 0.0   # the failed attempt earns nothing
    else:
        credit = None   # uncertain → tracked, no policy update (§12.35)
    return {
        "attempt_id": attempt.attempt_id,
        "specialist_role": attempt.specialist_role,
        "decision_id": attempt.decision_id,
        "reward": credit,
        "note": ("credit attaches to the ATTEMPT identity through the "
                 "durable reward event — not to the workflow"),
    }


def human_supplied_resolution(*, failed_attempt_id: str,
                              resolver: str) -> Dict[str, Any]:
    """§15.22 acceptance: when a HUMAN resolves what a specialist could
    not (e.g. supplying the working connector), the failed attempt
    receives NO positive credit — the resolution is recorded as
    human-provided and stays OUTSIDE the learned policy."""
    return {
        "failed_attempt_id": failed_attempt_id,
        "resolved_by": resolver,
        "resolution": "human_supplied",
        "credit_to_failed_attempt": 0.0,
        "policy_update": False,
        "note": ("a human-supplied resolution never retroactively "
                 "credits the failed specialist"),
    }


def branch_independence(*, branches: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Each branch's verdict stands alone: one branch's failure does
    not downgrade another branch's verified success."""
    out = []
    for b in branches or []:
        out.append({"branch_id": b.get("branch_id"),
                    "verdict": b.get("verdict"),
                    "independent": True})
    return {"branches": out,
            "note": ("verdicts are per branch — no cross-branch "
                     "blame or credit")}
