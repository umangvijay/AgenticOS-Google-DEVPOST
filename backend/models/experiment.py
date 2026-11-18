"""Controlled self-improvement experiments (Phase 13).

Improve future behavior through EVALUATED, REVERSIBLE changes. One record
per candidate change (prompt, routing policy, connector, implementation)
with:

- Immutable inputs: baseline/candidate refs, the held-out dataset hash,
  and a grading hash over the thresholds — pinned at proposal time so
  grading rules cannot be rewritten mid-flight.
- A state machine with SEPARATED permissions: proposal, evaluation,
  approval, activation, and rollback are distinct roles. Agents may
  propose but can never approve, promote, or roll back — and agents can
  never modify their own authorization or grading rules.
- Predeclared thresholds (success floor, regression ceiling, cost
  ceiling, critical constraints, bounded canary cohort) enforced
  deterministically at promotion: a candidate that improves AVERAGE
  quality but violates a critical constraint cannot promote.
- Promotion evidence: every promotion links its dataset, results, actor,
  and deployment version; rollback restores the prior ref.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ExperimentKind(str, Enum):
    PROMPT = "prompt"
    POLICY = "policy"
    CONNECTOR = "connector"
    IMPLEMENTATION = "implementation"


class ExperimentState(str, Enum):
    PROPOSED = "PROPOSED"
    EVALUATING = "EVALUATING"
    EVALUATED = "EVALUATED"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"
    CANCELLED = "CANCELLED"


TERMINAL_EXPERIMENT_STATES = frozenset({
    ExperimentState.PROMOTED, ExperimentState.REJECTED,
    ExperimentState.ROLLED_BACK, ExperimentState.CANCELLED,
})

# Permission roles (§13.4): separate proposal, evaluation, approval,
# activation, and rollback.
ROLE_PROPOSER = "proposer"
ROLE_EVALUATOR = "evaluator"
ROLE_APPROVER = "approver"
ROLE_ACTIVATOR = "activator"
ROLE_OPERATOR = "operator"

ALLOWED_EXPERIMENT_TRANSITIONS: Dict[ExperimentState, Dict[ExperimentState, set]] = {
    ExperimentState.PROPOSED: {
        ExperimentState.EVALUATING: {ROLE_EVALUATOR},
        ExperimentState.CANCELLED: {ROLE_PROPOSER, ROLE_OPERATOR},
    },
    ExperimentState.EVALUATING: {
        ExperimentState.EVALUATED: {ROLE_EVALUATOR},
        ExperimentState.CANCELLED: {ROLE_OPERATOR},
    },
    ExperimentState.EVALUATED: {
        ExperimentState.SHADOW: {ROLE_ACTIVATOR},
        ExperimentState.REJECTED: {ROLE_APPROVER},
        ExperimentState.CANCELLED: {ROLE_OPERATOR},
    },
    ExperimentState.SHADOW: {
        ExperimentState.CANARY: {ROLE_ACTIVATOR},
        ExperimentState.REJECTED: {ROLE_APPROVER},
        ExperimentState.CANCELLED: {ROLE_OPERATOR},
    },
    ExperimentState.CANARY: {
        ExperimentState.PROMOTED: {ROLE_APPROVER},
        ExperimentState.REJECTED: {ROLE_APPROVER},
        ExperimentState.CANCELLED: {ROLE_OPERATOR},
    },
    ExperimentState.PROMOTED: {
        ExperimentState.ROLLED_BACK: {ROLE_OPERATOR},
    },
    # Terminal
    ExperimentState.REJECTED: {},
    ExperimentState.ROLLED_BACK: {},
    ExperimentState.CANCELLED: {},
}


def experiment_transition_valid(current: ExperimentState,
                                target: ExperimentState,
                                role: str) -> bool:
    allowed = ALLOWED_EXPERIMENT_TRANSITIONS.get(current, {})
    return role in allowed.get(target, set())


class ExperimentTransitionError(Exception):
    def __init__(self, message: str, current: ExperimentState,
                 state_version: int):
        super().__init__(message)
        self.current = current
        self.state_version = state_version


def is_agent_actor(actor: str) -> bool:
    """§13.4: agents cannot approve, promote, or roll back, and cannot
    modify their own authorization or grading rules."""
    return str(actor or "").strip().lower().startswith("agent:")


# Targets agents may never propose changes to: their own authorization
# surface and the grading rules themselves.
_PROTECTED_TARGET_PREFIXES = ("authorization", "grading", "permission")


def agent_proposal_allowed(kind: ExperimentKind, target: str) -> bool:
    t = str(target or "").strip().lower()
    return not any(t.startswith(p) for p in _PROTECTED_TARGET_PREFIXES)


@dataclass
class ExperimentThresholds:
    """Predeclared BEFORE evaluation (§13.3): success, regression, cost,
    and rollback thresholds — plus the critical constraints that veto
    promotion regardless of average quality."""
    success_min: float = 0.0            # candidate success must be >= this
    regression_max: float = 0.0         # any listed metric may not degrade more
    regression_metrics: tuple = ("success",)
    # §13.12: per-metric direction — these metrics regress when HIGHER.
    regression_metrics_lower_is_better: tuple = ()
    cost_max_usd: Optional[float] = None
    max_cohort_fraction: float = 0.1    # §13.3: bounded canary cohort
    max_cohort_size: int = 50
    critical_constraints: tuple = ()    # named checks; ANY violation vetoes
    # §13.18: evidence requirements before each exposure increase —
    # DECLARED at proposal (0 = the stage gate is not armed; like
    # success_min, operators predeclare the sufficiency bar).
    min_shadow_observations: int = 0    # measured candidate executions
    min_canary_exposures: int = 0       # counted cohort admissions

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success_min": self.success_min,
            "regression_max": self.regression_max,
            "regression_metrics": list(self.regression_metrics),
            "regression_metrics_lower_is_better":
                list(self.regression_metrics_lower_is_better),
            "cost_max_usd": self.cost_max_usd,
            "max_cohort_fraction": self.max_cohort_fraction,
            "max_cohort_size": self.max_cohort_size,
            "critical_constraints": list(self.critical_constraints),
            "min_shadow_observations": self.min_shadow_observations,
            "min_canary_exposures": self.min_canary_exposures,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ExperimentThresholds":
        data = data or {}
        return cls(
            success_min=float(data.get("success_min", 0.0)),
            regression_max=float(data.get("regression_max", 0.0)),
            regression_metrics=tuple(data.get("regression_metrics") or ("success",)),
            regression_metrics_lower_is_better=tuple(
                data.get("regression_metrics_lower_is_better") or ()),
            cost_max_usd=(float(data["cost_max_usd"])
                          if data.get("cost_max_usd") is not None else None),
            max_cohort_fraction=float(data.get("max_cohort_fraction", 0.1)),
            max_cohort_size=int(data.get("max_cohort_size", 50)),
            critical_constraints=tuple(data.get("critical_constraints") or ()),
            min_shadow_observations=int(data.get("min_shadow_observations", 0)),
            min_canary_exposures=int(data.get("min_canary_exposures", 0)),
        )


@dataclass
class Experiment:
    experiment_id: str
    kind: ExperimentKind
    target: str                       # what it modifies (prompt id, policy name, connector id)
    hypothesis: str
    expected_benefit: str
    baseline_ref: str                 # immutable version refs
    candidate_ref: str
    dataset_ref: str                  # held-out set identity
    dataset_hash: str                 # pinned at proposal (§13.2)
    grading_hash: str                 # hash over thresholds — grading rules
    thresholds_json: str
    state: ExperimentState = ExperimentState.PROPOSED
    state_version: int = 1
    candidate_diff: str = ""          # reviewable diff (§13.5)
    results_json: str = "{}"          # baseline vs candidate metrics
    manifest_json: str = "{}"         # §13.10 pinned artifact identities
    proposed_by: str = ""
    evaluated_by: str = ""
    approved_by: str = ""
    activated_by: str = ""
    rolled_back_by: str = ""
    cohort_size: Optional[int] = None
    promoted_dataset_hash: Optional[str] = None
    promoted_results_json: Optional[str] = None
    promoted_actor: Optional[str] = None
    deployment_version: Optional[str] = None
    rollback_reason: Optional[str] = None
    created_at: int = 0
    updated_at: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_EXPERIMENT_STATES

    @property
    def rejection_reason(self) -> Optional[str]:
        import json
        rejection = json.loads(self.results_json or "{}").get("rejection") or {}
        return rejection.get("reason")

    # ── 30.31 candidate registry fields ──────────────────────────────
    # Immutable lineage/mask/reward/split identities ride the PINNED
    # manifest (§13.10) so they survive restart on every store without a
    # schema migration, while staying typed and validated here.

    @property
    def manifest(self) -> Dict[str, Any]:
        import json
        try:
            data = json.loads(self.manifest_json or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _manifest_set(self, key: str, value: Any) -> None:
        import json
        data = self.manifest
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        self.manifest_json = json.dumps(data, sort_keys=True)

    @property
    def predecessor_id(self) -> Optional[str]:
        """The candidate this one was derived from (rollback target)."""
        return self.manifest.get("predecessor_id")

    @predecessor_id.setter
    def predecessor_id(self, value: Optional[str]) -> None:
        self._manifest_set("predecessor_id", value)

    @property
    def policy_kind(self) -> Optional[str]:
        return self.manifest.get("policy_kind")

    @policy_kind.setter
    def policy_kind(self, value: Optional[str]) -> None:
        self._manifest_set("policy_kind", value)

    @property
    def policy_digest(self) -> Optional[str]:
        return self.manifest.get("policy_digest")

    @policy_digest.setter
    def policy_digest(self, value: Optional[str]) -> None:
        self._manifest_set("policy_digest", value)

    @property
    def split_ref(self) -> Optional[str]:
        """Frozen consented split identity used for training/evaluation."""
        return self.manifest.get("split_ref")

    @split_ref.setter
    def split_ref(self, value: Optional[str]) -> None:
        self._manifest_set("split_ref", value)

    @property
    def mask_version(self) -> Optional[str]:
        return self.manifest.get("mask_version")

    @mask_version.setter
    def mask_version(self, value: Optional[str]) -> None:
        self._manifest_set("mask_version", value)

    @property
    def reward_version(self) -> Optional[str]:
        return self.manifest.get("reward_version")

    @reward_version.setter
    def reward_version(self, value: Optional[str]) -> None:
        self._manifest_set("reward_version", value)

    @property
    def stop_reason(self) -> Optional[str]:
        """Why rollout stopped (threshold breach, denial, interruption)."""
        return self.manifest.get("stop_reason")

    @stop_reason.setter
    def stop_reason(self, value: Optional[str]) -> None:
        self._manifest_set("stop_reason", value)

    def to_row(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id, "kind": self.kind.value,
            "target": self.target, "hypothesis": self.hypothesis,
            "expected_benefit": self.expected_benefit,
            "baseline_ref": self.baseline_ref,
            "candidate_ref": self.candidate_ref,
            "candidate_diff": self.candidate_diff,
            "dataset_ref": self.dataset_ref,
            "dataset_hash": self.dataset_hash,
            "grading_hash": self.grading_hash,
            "thresholds_json": self.thresholds_json,
            "state": self.state.value, "state_version": self.state_version,
            "results_json": self.results_json,
            "manifest_json": self.manifest_json,
            "proposed_by": self.proposed_by, "evaluated_by": self.evaluated_by,
            "approved_by": self.approved_by, "activated_by": self.activated_by,
            "rolled_back_by": self.rolled_back_by,
            "cohort_size": self.cohort_size,
            "promoted_dataset_hash": self.promoted_dataset_hash,
            "promoted_results_json": self.promoted_results_json,
            "promoted_actor": self.promoted_actor,
            "deployment_version": self.deployment_version,
            "rollback_reason": self.rollback_reason,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }
