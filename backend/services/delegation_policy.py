"""Delegation policy (Phase 15 §15.0/§15.1) — the DETERMINISTIC baseline
for when delegation is worthwhile, before any agent-selection learning.

- §15.0: the specialist INVENTORY — every advertised role's execution
  path, contract shape, and placeholder status, queryable so "we have a
  verifier" is a checkable claim, not an assertion.
- §15.1: WHEN to delegate. Delegate when the task requires a distinct
  capability, independent verification, a meaningful dependency, or
  safely parallel work. Do NOT delegate when coordination overhead
  outweighs the outcome. Every decision records WHY and which
  alternatives were considered — the platform can later compare
  delegated execution against its ordinary baseline (§15.23).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.models.agent_task import AgentRole


# ── §15.0: the inventory ───────────────────────────────────────────────

def specialist_inventory() -> List[Dict[str, Any]]:
    """Every advertised specialist with its REAL execution path. A role
    whose runner is None is executed by its dedicated service (named);
    a role with no path at all is a PLACEHOLDER — visible, never
    advertised as callable."""
    from backend.services.agent_task_contracts import SPECIALIST_REGISTRY
    inventory = []
    for role, entry in SPECIALIST_REGISTRY.items():
        runner = entry.get("runner")
        service = entry.get("service")
        callable_role = bool(runner or service or role in _SERVICE_EXECUTED)
        inventory.append({
            "role": role.value,
            "title": entry.get("title"),
            "authority_boundary": entry.get("boundary"),
            "output_schema": entry.get("output_schema"),
            "runner": runner,
            "execution_path": ("runner:" + runner) if runner else
                              ("service:" + str(service or "dedicated service"))
                              if callable_role else None,
            "callable": callable_role,
            "placeholder": not callable_role,
        })
    return inventory


# Roles whose work flows through their dedicated deterministic service
# (documented, not runner functions).
_SERVICE_EXECUTED = {
    AgentRole.RECONCILIATION,        # effect reconciliation service
    AgentRole.EVALUATION_CURATOR,    # evaluation curation service
    AgentRole.RELEASE_VERIFIER,      # release evidence assembler
    # ── Phase 17 §17.19: deliverable specialists ───────────────────────
    AgentRole.DELIVERABLE_ASSEMBLER,     # the artifact builder pipeline
    AgentRole.PROVENANCE_REVIEWER,       # provenance services
    AgentRole.DOCUMENT_QUALITY_REVIEWER, # the rendered-document verifier
    AgentRole.BUILD_VERIFIER,            # build-evidence interpretation
    AgentRole.DELIVERY_RECONCILER,       # the §5 reconciliation service
}


# ── §15.1: the deterministic delegation decision ───────────────────────

@dataclass
class DelegationDecision:
    delegate: bool
    reason: str
    alternatives_considered: List[str] = field(default_factory=list)
    suggested_role: Optional[AgentRole] = None
    parallelizable: bool = False


def should_delegate(*, task_category: str = "workflow",
                    requires_verification: bool = False,
                    requires_distinct_capability: bool = False,
                    has_dependencies: bool = False,
                    parallelizable: bool = False,
                    read_only: bool = False,
                    simple_lookup: bool = False) -> DelegationDecision:
    """The deterministic delegation baseline (§15.1). Simple tasks stay
    simple: a read-only lookup with no verification need, dependencies,
    or parallelism runs inline. Complex tasks get the appropriate
    specialist. The reason + alternatives ALWAYS travel with the
    decision (recorded on the contract)."""
    alternatives: List[str] = ["inline_execution"]
    if simple_lookup and not (requires_verification or has_dependencies
                              or parallelizable
                              or requires_distinct_capability):
        return DelegationDecision(
            delegate=False,
            reason="simple task: inline execution beats coordination "
                   "overhead (§15.1)",
            alternatives_considered=alternatives)
    if requires_distinct_capability:
        alternatives.append("capability_build")
        return DelegationDecision(
            delegate=True,
            reason="task requires a distinct capability the inline path "
                   "does not have",
            alternatives_considered=alternatives,
            suggested_role=AgentRole.CONNECTOR_VALIDATOR
            if task_category == "connector" else None,
            parallelizable=parallelizable)
    if requires_verification:
        alternatives.append("self_verification")
        return DelegationDecision(
            delegate=True,
            reason="independent verification required — the producer "
                   "cannot grade its own output (§15.14)",
            alternatives_considered=alternatives,
            suggested_role=AgentRole.OUTCOME_VERIFIER,
            parallelizable=False)
    if parallelizable and not read_only:
        alternatives.append("sequential_execution")
        return DelegationDecision(
            delegate=True,
            reason="independent parallel work — bounded specialists use "
                   "capacity the sequential path would waste",
            alternatives_considered=alternatives,
            parallelizable=True)
    if has_dependencies:
        alternatives.append("single_agent_plan")
        return DelegationDecision(
            delegate=True,
            reason="meaningful dependency chain — each stage gets a "
                   "bounded contract with its own acceptance criteria",
            alternatives_considered=alternatives)
    return DelegationDecision(
        delegate=False,
        reason="no distinct capability, verification need, dependency, "
               "or parallelism — delegation adds overhead without "
               "improving the outcome",
        alternatives_considered=alternatives)


# ── §15.24: learned specialist selection (Phase 14's controlled path) ──

def eligible_specialist_actions(role: AgentRole) -> List[str]:
    """§15.24: the action set for learned selection is SMALL and TYPED —
    the registry's roles that are actually callable. It is derived from
    the SAME inventory §15.0 publishes; a role that is not callable is
    never selectable."""
    return [e["role"] for e in specialist_inventory() if e["callable"]]


async def select_specialist(policy, *, task_category: str,
                            capability: str = "",
                            input_size_bytes: int = 0,
                            candidates: Optional[List[str]] = None,
                            rng=None) -> Dict[str, Any]:
    """Learned specialist selection through the Phase 14 contextual
    policy (release-scoped upstream): candidates arrive pre-filtered to
    CALLABLE roles (§15.24 — learned selection cannot choose an
    unvalidated implementation), the selection probability is logged
    exactly, and authority/limits are UNCHANGED — the delegation layer
    enforces them regardless of who selected."""
    from backend.services.contextual_policy import ContextualRoutingPolicy
    if not isinstance(policy, ContextualRoutingPolicy):
        raise ValueError("specialist selection requires the contextual "
                         "policy (§15.24)")
    eligible = candidates or eligible_specialist_actions(
        AgentRole.OUTCOME_VERIFIER)   # the caller narrows by task
    decision = await policy.select(
        candidates=eligible, task_category=task_category,
        capability=capability, input_size_bytes=input_size_bytes, rng=rng)
    return {"selected_role": decision.selected,
            "mechanism": decision.mechanism,
            "selection_probability": decision.selection_probability,
            "ranked": decision.ranked,
            "note": ("authority and delegation limits are enforced at "
                     "the contract layer regardless of this selection")}
