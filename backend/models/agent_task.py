"""Agent task contracts (Phase 15 §15.4–15.6).

Every delegated task carries: a parent, typed inputs, an output schema,
allowed tools, a budget, a deadline, cancellation behavior, and an
ownership lease. Delegation is BOUNDED (§15.5): depth, per-parent
fan-out, concurrent children, and cumulative spend are enforced at
creation — and every role's allowed tools are restricted to its
authority boundary (§15.1–15.3): the verifier receives evidence without
the authority to repeat actions; the validator runs approved tests only;
the recovery analyst cannot broaden permissions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AgentRole(str, Enum):
    OUTCOME_VERIFIER = "outcome_verifier"
    CONNECTOR_VALIDATOR = "connector_validator"
    RECOVERY_ANALYST = "recovery_analyst"
    BROWSER_NAVIGATOR = "browser_navigator"
    DOM_RESOLVER = "dom_resolver"
    RECONCILIATION = "reconciliation"
    EVALUATION_CURATOR = "evaluation_curator"
    IMPROVEMENT_PROPOSER = "improvement_proposer"
    RELEASE_VERIFIER = "release_verifier"
    # ── Phase 17 §17.19: deliverable specialists ───────────────────────
    DELIVERABLE_ASSEMBLER = "deliverable_assembler"
    PROVENANCE_REVIEWER = "provenance_reviewer"
    DOCUMENT_QUALITY_REVIEWER = "document_quality_reviewer"
    BUILD_VERIFIER = "build_verifier"
    DELIVERY_RECONCILER = "delivery_reconciler"


# §15.1–15.3: authority boundaries — the allowed-tools whitelist per role.
# Anything not listed is refused at contract creation, and the list is
# deliberately smaller than the tool router's full catalog.
ROLE_ALLOWED_TOOLS: Dict[AgentRole, set] = {
    AgentRole.OUTCOME_VERIFIER: {"evidence.read", "artifact.resolve",
                                 "criteria.verify"},
    AgentRole.CONNECTOR_VALIDATOR: {"connector.read", "connector.test",
                                    "evidence.read"},
    AgentRole.RECOVERY_ANALYST: {"evidence.read", "recovery.propose"},
    AgentRole.BROWSER_NAVIGATOR: {"browser.observe", "browser.act"},
    AgentRole.DOM_RESOLVER: {"browser.observe"},
    AgentRole.RECONCILIATION: {"evidence.read", "effect.reconcile"},
    AgentRole.EVALUATION_CURATOR: {"evidence.read", "dataset.propose"},
    AgentRole.IMPROVEMENT_PROPOSER: {"evidence.read", "experiment.propose"},
    AgentRole.RELEASE_VERIFIER: {"evidence.read", "release.assemble"},
    AgentRole.DELIVERABLE_ASSEMBLER: {"artifact.resolve", "evidence.read",
                                      "artifact.render"},
    AgentRole.PROVENANCE_REVIEWER: {"evidence.read"},
    AgentRole.DOCUMENT_QUALITY_REVIEWER: {"artifact.resolve",
                                          "evidence.read"},
    AgentRole.BUILD_VERIFIER: {"evidence.read", "build.evidence.read"},
    AgentRole.DELIVERY_RECONCILER: {"evidence.read", "effect.reconcile"},
}


class AgentTaskState(str, Enum):
    PROPOSED = "PROPOSED"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


TERMINAL_AGENT_TASK_STATES = frozenset({
    AgentTaskState.COMPLETED, AgentTaskState.FAILED,
    AgentTaskState.TIMED_OUT, AgentTaskState.CANCELLED,
    AgentTaskState.SUPERSEDED,
})

ALLOWED_AGENT_TASK_TRANSITIONS: Dict[AgentTaskState, Dict[AgentTaskState, set]] = {
    AgentTaskState.PROPOSED: {
        AgentTaskState.ASSIGNED: {"system"},
        # A child can fail or time out before it ever ran (refused at
        # spawn, queue loss) — honest terminal states, not silent drops.
        AgentTaskState.FAILED: {"system"},
        AgentTaskState.TIMED_OUT: {"system"},
        AgentTaskState.CANCELLED: {"system", "operator", "parent"},
        AgentTaskState.SUPERSEDED: {"system"},
    },
    AgentTaskState.ASSIGNED: {
        AgentTaskState.RUNNING: {"system"},
        # Inline/synchronous roles complete without a RUNNING heartbeat.
        AgentTaskState.COMPLETED: {"system"},
        AgentTaskState.FAILED: {"system"},
        AgentTaskState.TIMED_OUT: {"system"},
        AgentTaskState.CANCELLED: {"system", "operator", "parent"},
        AgentTaskState.SUPERSEDED: {"system"},
    },
    AgentTaskState.RUNNING: {
        AgentTaskState.COMPLETED: {"system"},
        AgentTaskState.FAILED: {"system"},
        AgentTaskState.TIMED_OUT: {"system"},
        AgentTaskState.CANCELLED: {"system", "operator", "parent"},
    },
    # Terminal
    AgentTaskState.COMPLETED: {},
    AgentTaskState.FAILED: {},
    AgentTaskState.TIMED_OUT: {},
    AgentTaskState.CANCELLED: {},
    AgentTaskState.SUPERSEDED: {},
}


def agent_task_transition_valid(current: AgentTaskState,
                                target: AgentTaskState) -> bool:
    return target in ALLOWED_AGENT_TASK_TRANSITIONS.get(current, {})


class AgentTaskError(Exception):
    def __init__(self, message: str, code: str = "AGENT_TASK_ERROR"):
        super().__init__(message)
        self.code = code


@dataclass
class AgentTaskContract:
    contract_id: str
    parent_kind: str                     # run | contract
    parent_id: str
    ancestor_ids: List[str] = field(default_factory=list)   # cycle detection
    role: AgentRole = AgentRole.OUTCOME_VERIFIER
    tenant_id: str = ""
    dedup_key: Optional[str] = None      # duplicate assignment dedup (§15 acceptance)
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)  # required keys
    allowed_tools: List[str] = field(default_factory=list)
    budget_units: int = 0
    deadline_at: int = 0
    depth: int = 0
    completion_policy: str = "require_all"   # require_all | require_any | best_effort
    # ── §15.3: the complete delegation contract ────────────────────────
    goal: str = ""                       # what the specialist should accomplish
    exclusions: List[str] = field(default_factory=list)   # what it must NOT do
    input_refs: List[str] = field(default_factory=list)   # authorized evidence/artifacts
    required_capabilities: List[str] = field(default_factory=list)
    acceptance_criteria: List[Dict[str, Any]] = field(default_factory=list)
    retry_policy: str = "bounded"        # bounded | none | escalate
    agent_version: str = ""              # reproducibility (§15.3)
    tool_version: str = ""
    policy_version: str = ""
    # ── lifecycle ──────────────────────────────────────────────────────
    state: AgentTaskState = AgentTaskState.PROPOSED
    state_version: int = 1
    owner_worker_id: Optional[str] = None
    lease_expires_at: int = 0
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    spent_units: int = 0
    created_at: int = 0
    updated_at: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_AGENT_TASK_STATES

    def to_row(self) -> Dict[str, Any]:
        import json
        return {
            "contract_id": self.contract_id,
            "parent_kind": self.parent_kind, "parent_id": self.parent_id,
            "ancestor_ids_json": json.dumps(self.ancestor_ids),
            "goal": self.goal,
            "exclusions_json": json.dumps(self.exclusions),
            "input_refs_json": json.dumps(self.input_refs),
            "required_capabilities_json": json.dumps(self.required_capabilities),
            "acceptance_criteria_json": json.dumps(self.acceptance_criteria),
            "retry_policy": self.retry_policy,
            "agent_version": self.agent_version,
            "tool_version": self.tool_version,
            "policy_version": self.policy_version,
            "role": self.role.value, "tenant_id": self.tenant_id,
            "dedup_key": self.dedup_key,
            "input_data_json": json.dumps(self.input_data),
            "output_schema_json": json.dumps(self.output_schema),
            "allowed_tools_json": json.dumps(self.allowed_tools),
            "budget_units": int(self.budget_units),
            "deadline_at": int(self.deadline_at),
            "depth": int(self.depth),
            "completion_policy": self.completion_policy,
            "state": self.state.value, "state_version": self.state_version,
            "owner_worker_id": self.owner_worker_id,
            "lease_expires_at": int(self.lease_expires_at or 0),
            "result_json": json.dumps(self.result) if self.result else None,
            "error": self.error, "spent_units": int(self.spent_units),
            "created_at": int(self.created_at or 0),
            "updated_at": int(self.updated_at or 0),
        }


# §15.5: bounded delegation — hard caps enforced at creation.
def validate_contract_completeness(contract: "AgentTaskContract") -> None:
    """§15.3: a specialist cannot START with an incomplete contract —
    authority (allowed tools) and success criteria (acceptance
    criteria + typed output schema) must be unambiguous. Called at
    delegation creation, before any claim can happen."""
    problems = []
    if not (contract.allowed_tools or []):
        problems.append("no allowed_tools (authority is ambiguous)")
    if not (contract.acceptance_criteria or []):
        problems.append("no acceptance_criteria (success is ambiguous)")
    if not contract.goal and not (contract.input_data or {}):
        problems.append("neither goal nor input_data (the task is empty)")
    if problems:
        raise AgentTaskError(
            "Incomplete delegation contract: " + "; ".join(problems),
            "INCOMPLETE_CONTRACT")


MAX_DELEGATION_DEPTH = 3
MAX_CHILDREN_PER_PARENT = 8
MAX_ACTIVE_CHILDREN_PER_PARENT = 4


def validate_output_schema(result: Optional[Dict[str, Any]],
                           schema: Dict[str, Any]) -> List[str]:
    """Typed outputs (§15.4): required keys present, no unexpected None
    for required fields. Returns the list of violations (empty = valid)."""
    if not schema:
        return []
    missing = [k for k in (schema.get("required") or [])
               if not isinstance(result, dict) or result.get(k) is None]
    return missing


def validate_task_settlement(target: AgentTaskState, result: Optional[Dict[str, Any]],
                             schema: Dict[str, Any], spent: int, delta: int, budget: int) -> None:
    """Validate against the locked contract before committing an outcome or charging units."""
    if target is AgentTaskState.COMPLETED or result is not None:
        missing = validate_output_schema(result, schema)
        if missing:
            raise AgentTaskError(f"Result violates the output schema: missing required {', '.join(missing)}",
                                 "OUTPUT_SCHEMA_VIOLATION")
    if type(delta) is not int or delta < 0 or (delta and spent + delta > budget):
        raise AgentTaskError("Contract budget exceeded or invalid spend", "BUDGET_EXCEEDED")
