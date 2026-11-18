"""Routing decision log (Phase 14).

Contextual-bandit foundation (§14.4): every model-selection decision is
logged with its context, the ELIGIBLE actions (forbidden actions can
never enter the candidate set — §14.3), the chosen action, the selection
probability, and the policy version. The verified outcome joins LATER
(§14.1: a model call finishing without exception is not proof of task
success; §12.35: an unverified "completed" flag cannot become a positive
learning reward) — feedback carries an explicit outcome class:

    verified | partial | uncertain | rejected | missing

Missing/late feedback is represented, never silently dropped (§14.4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RewardClass(str, Enum):
    VERIFIED = "verified"        # acceptance criteria passed + effects confirmed
    PARTIAL = "partial"          # completed but needs_review / partial effects
    UNCERTAIN = "uncertain"      # uncertain/failed effects leave the outcome unknown
    REJECTED = "rejected"        # failed verification or failed task
    MISSING = "missing"          # no feedback joined yet (explicit, not zero)


# §14.11: the context schema — the KNOWN task categories the contextual
# policy buckets on. An unknown category is coerced to the baseline
# bucket rather than fragmenting per-context stats into singletons.
KNOWN_TASK_CATEGORIES = (
    "workflow", "research", "browser", "resume", "communication",
    "data", "improvement",
    "verification",              # §15.22: specialist attempts attribute here
    "specialist_delegation",     # §15.24: learned specialist selection
)


class RoutingContextError(ValueError):
    pass


def validate_context(*, task_category: str = "workflow",
                     capability: str = "",
                     input_size_bytes: int = 0,
                     latency_target_ms: Optional[int] = None,
                     budget_units: Optional[int] = None,
                     selection_probability: Optional[float] = None) -> Dict[str, Any]:
    """§14.11: validate the decision's CONTEXT fields. Constraints:
    category from the known set (an unknown one raises — the caller
    decides, never silent bucket invention), non-negative sizes/budgets,
    positive latency targets, probability in [0, 1]. Returns the
    normalized values."""
    category = str(task_category or "workflow")
    if category not in KNOWN_TASK_CATEGORIES:
        raise RoutingContextError(
            f"Unknown task category '{category}' — the context schema "
            f"buckets on {KNOWN_TASK_CATEGORIES}")
    size = int(input_size_bytes or 0)
    if size < 0:
        raise RoutingContextError("input_size_bytes cannot be negative")
    if latency_target_ms is not None and int(latency_target_ms) <= 0:
        raise RoutingContextError("latency_target_ms must be positive")
    if budget_units is not None and int(budget_units) < 0:
        raise RoutingContextError("budget_units cannot be negative")
    if selection_probability is not None:
        p = float(selection_probability)
        if not (0.0 <= p <= 1.0):
            raise RoutingContextError(
                f"selection_probability must be in [0, 1], got {p}")
    return {"task_category": category, "capability": str(capability or ""),
            "input_size_bytes": size,
            "latency_target_ms": latency_target_ms,
            "budget_units": budget_units}


@dataclass
class RoutingDecision:
    decision_id: str
    run_id: str
    task_id: Optional[str]
    # §14.2 context: task class, capability, input size, latency need, budget.
    task_category: str = "workflow"
    capability: str = ""
    input_size_bytes: int = 0
    latency_target_ms: Optional[int] = None
    budget_units: Optional[int] = None
    # §14.3/§14.4: eligible set + choice + probability + policy version.
    eligible_actions: List[str] = field(default_factory=list)
    selected_action: str = ""
    # §13.6: the selection mechanism that ACTUALLY ran — a deterministic
    # ranked first-fit choice is NOT uniform-random sampling; its honest
    # logging probability is 1.0 (the policy always picks the ranked top).
    selection_mechanism: str = "ranked_first_fit"
    selection_probability: float = 1.0
    latency_ms: Optional[float] = None   # measured; None = unavailable
    policy_applied: int = 0              # reward applied to model_stats?
    policy_version: str = "ucb1-v1"
    # Provider fallback lineage (§12.15): the action actually charged may
    # differ from the first selected candidate.
    resolved_action: Optional[str] = None
    fallback_from: Optional[str] = None
    # §14.1 delayed verified feedback.
    reward_class: str = RewardClass.MISSING.value
    reward_verified_success: Optional[int] = None   # 1/0/None
    feedback_at: Optional[int] = None
    evaluation_id: Optional[str] = None
    created_at: int = 0

    def to_row(self) -> Dict[str, Any]:
        # §14.11: every persisted row passes the context schema.
        validate_context(
            task_category=self.task_category,
            input_size_bytes=self.input_size_bytes,
            latency_target_ms=self.latency_target_ms,
            budget_units=self.budget_units,
            selection_probability=self.selection_probability)
        return {
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "task_category": self.task_category,
            "capability": self.capability,
            "input_size_bytes": int(self.input_size_bytes or 0),
            "latency_target_ms": self.latency_target_ms,
            "budget_units": self.budget_units,
            "eligible_actions_json": json_dumps(self.eligible_actions),
            "selected_action": self.selected_action,
            "selection_mechanism": self.selection_mechanism,
            "selection_probability": float(self.selection_probability),
            "latency_ms": self.latency_ms,
            "policy_applied": int(self.policy_applied),
            "policy_version": self.policy_version,
            "resolved_action": self.resolved_action,
            "fallback_from": self.fallback_from,
            "reward_class": self.reward_class,
            "reward_verified_success": self.reward_verified_success,
            "feedback_at": self.feedback_at,
            "evaluation_id": self.evaluation_id,
            "created_at": int(self.created_at or 0),
        }


def json_dumps(value: Any) -> str:
    import json
    return json.dumps(value or [])


def outcome_class_for(evaluation: Dict[str, Any]) -> str:
    """§14.1: map a durable evaluation record to the reward class. An
    unverified 'completed' flag can NEVER map to positive reward."""
    if not evaluation:
        return RewardClass.MISSING.value
    if evaluation.get("success"):
        return RewardClass.VERIFIED.value
    needs_review = evaluation.get("needs_review_tasks") or []
    uncertain = (evaluation.get("uncertain_effects") or
                 evaluation.get("unverified_effects") or [])
    partial = evaluation.get("partially_applied_effects") or []
    if partial:
        return RewardClass.PARTIAL.value
    if needs_review or uncertain:
        return RewardClass.UNCERTAIN.value
    return RewardClass.REJECTED.value
