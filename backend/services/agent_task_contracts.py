"""Agent task contracts — the specialist delegation integration
(Phase 15; the roadmap-named file).

ONE service through which every specialist delegation flows:

- Task contracts carry the §15.4 completeness (parent, typed inputs/
  outputs, allowed tools, budget, deadline, cancellation, dedup).
- The SPECIALISTS are the roadmap's roles: Outcome Verifier, Connector
  Validator, Recovery Analyst (the existing recovery agent), and the
  later additions — each with its authority boundary enforced by the
  role-tool whitelist and the deterministic gates.
- Execution is bounded: depth/fan-out/concurrency/spend caps from the
  repository, typed output validation, and lease-fenced claims.
- The deterministic services (authorization, budget, effect ledger)
  stay OUTSIDE the agents — agents propose; deterministic code decides.

This module re-exports the contract primitives and provides the
SPECIALIST_REGISTRY: role → (runner, typed output schema, default
tools), so callers delegate by role name without touching internals.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.models.agent_task import (  # noqa: F401 (re-export)
    MAX_ACTIVE_CHILDREN_PER_PARENT,
    MAX_CHILDREN_PER_PARENT,
    MAX_DELEGATION_DEPTH,
    AgentRole,
    AgentTaskContract,
    AgentTaskError,
    AgentTaskState,
    ROLE_ALLOWED_TOOLS,
    validate_output_schema,
)
from backend.repositories.agent_task_repository import (  # noqa: F401
    AgentTaskRepository,
    get_agent_task_repo,
    new_contract_id,
)
from backend.services.agent_contracts import (  # noqa: F401 (re-export)
    cancel,
    complete,
    delegate,
    fail,
)

# ── §13.28/§15: the specialist registry ────────────────────────────────

SPECIALIST_REGISTRY: Dict[AgentRole, Dict[str, Any]] = {
    AgentRole.OUTCOME_VERIFIER: {
        "title": "Outcome Verifier",
        "boundary": "read-only evidence access",
        "runner": "backend.agents.verifier.verifier_agent:"
                  "run_verifier_via_contract",
        "output_schema": {"required": ["verdict", "criterion_results",
                                       "reasoning"]},
        # §15.3: the registry's default completion criteria (the caller
        # may pass narrower ones; the contract is never ambiguous).
        "default_acceptance": [{"kind": "non_empty",
                                "description": "a structured verdict "
                                               "with per-criterion "
                                               "results exists"}],
    },
    AgentRole.CONNECTOR_VALIDATOR: {
        "title": "Connector Validator",
        "boundary": "proposes test cases; the §6 pipeline executes",
        "runner": "backend.agents.connector_validator.validator_agent:"
                  "run_validator_via_contract",
        "output_schema": {"required": ["operation_id", "test_cases"]},
        "default_acceptance": [{"kind": "non_empty", "description": "a test-case proposal for the exact operation exists"}],
    },
    AgentRole.RECOVERY_ANALYST: {
        "title": "Recovery Analyst",
        "boundary": "proposes bounded repairs; no permission escalation",
        "runner": "backend.workers.recovery_worker:RecoveryWorker",
        "output_schema": {"required": ["action"]},
        "default_acceptance": [{"kind": "non_empty", "description": "a bounded repair action is proposed"}],
    },
    AgentRole.RECONCILIATION: {
        "title": "Reconciliation Agent",
        "boundary": "gathers evidence for uncertain effects; no blind replay",
        "runner": None,   # effect reconciliation service executes directly
        "output_schema": {"required": ["effect_id", "resolution"]},
        "default_acceptance": [{"kind": "non_empty", "description": "a resolution with evidence exists"}],
    },
    AgentRole.EVALUATION_CURATOR: {
        "title": "Evaluation Curator",
        "boundary": "proposes dataset cases; cannot change held-out sets",
        "runner": None,
        "output_schema": {"required": ["proposed_cases"]},
        "default_acceptance": [{"kind": "non_empty", "description": "proposed cases are listed"}],
    },
    AgentRole.IMPROVEMENT_PROPOSER: {
        "title": "Improvement Proposer",
        "boundary": "drafts versioned changes; cannot self-promote",
        "runner": "backend.services.improvement_rollout:"
                  "propose_from_failure_clusters",
        "output_schema": {"required": ["hypothesis"]},
        "default_acceptance": [{"kind": "non_empty", "description": "a versioned hypothesis exists"}],
    },
    AgentRole.RELEASE_VERIFIER: {
        "title": "Release Verifier",
        "boundary": "assembles candidate evidence; cannot self-certify",
        "runner": None,
        "output_schema": {"required": ["checks", "summary"]},
        "default_acceptance": [{"kind": "non_empty", "description": "the checks list exists"}],
    },
    # ── Phase 17 §17.19: deliverable specialists ───────────────────────
    AgentRole.DELIVERABLE_ASSEMBLER: {
        "title": "Deliverable Assembler",
        "boundary": "assembles authorized inputs into the requested format; cannot self-verify",
        "runner": None,   # the artifact builder pipeline executes
        "output_schema": {"required": ["artifact_id", "manifest"]},
        "default_acceptance": [{"kind": "non_empty", "description": "the assembled manifest exists"}],
    },
    AgentRole.PROVENANCE_REVIEWER: {
        "title": "Provenance Reviewer",
        "boundary": "identifies unsupported or conflicting claims; cannot create authoritative facts",
        "runner": None,   # provenance services execute deterministically
        "output_schema": {"required": ["unsupported_claims", "conflicts"]},
        "default_acceptance": [{"kind": "non_empty", "description": "the claim review exists"}],
    },
    AgentRole.DOCUMENT_QUALITY_REVIEWER: {
        "title": "Document Quality Reviewer",
        "boundary": "identifies layout/readability issues; cannot override deterministic failures",
        "runner": None,
        "output_schema": {"required": ["layout_issues"]},
        "default_acceptance": [{"kind": "non_empty", "description": "the quality review exists"}],
    },
    AgentRole.BUILD_VERIFIER: {
        "title": "Build Verifier",
        "boundary": "interprets isolated build evidence; cannot execute outside the runner",
        "runner": None,
        "output_schema": {"required": ["build_state", "evidence"]},
        "default_acceptance": [{"kind": "non_empty", "description": "the build interpretation exists"}],
    },
    AgentRole.DELIVERY_RECONCILER: {
        "title": "Delivery Reconciler",
        "boundary": "resolves ambiguous external delivery; cannot blindly resend",
        "runner": None,   # the §5 effect reconciliation service executes
        "output_schema": {"required": ["effect_id", "resolution"]},
        "default_acceptance": [{"kind": "non_empty", "description": "the reconciliation exists"}],
    },
    # §28.5: the enabled DOM resolver previously had NO registry entry —
    # it could not be delegated through delegate_specialist (the normal
    # entry point) and needed the plain contract path. This entry makes
    # the enabled inventory delegable end to end; resolve_target still
    # executes directly (runner: None).
    AgentRole.DOM_RESOLVER: {
        "title": "DOM Resolver",
        "boundary": "resolves a semantic target against one observed page; refuses ambiguity; never dispatches actions",
        "runner": None,   # browser_target_resolver.resolve_target executes directly
        "service": "backend.services.browser_target_resolver:resolve_target",
        "output_schema": {"required": ["resolved_ref", "strategy", "ambiguous"]},
        "default_acceptance": [{"kind": "non_empty", "description": "a resolved target exists"}],
    },
}


def resolve_runner(role: AgentRole) -> Optional[str]:
    """The runner import path for a role (None = the role's work flows
    through its dedicated service, not a runner function)."""
    return SPECIALIST_REGISTRY.get(role, {}).get("runner")


async def record_selection_for_delegation(
        role: AgentRole, *, parent_id: str, tenant_id: str,
        candidates: Optional[List[str]] = None) -> Optional[str]:
    """§15.24 (finding F): when the 'routing' release stage is enabled,
    learned specialist selection runs through the Phase 14 contextual
    policy over the CALLABLE candidate roles, and the decision (with
    its logged probability) lands in the SAME decision log as model
    routing. Authority is untouched — this only OBSERVES and selects;
    the contract layer enforces everything."""
    try:
        from backend.services.improvement_governance import (
            contextual_routing_enabled)
        if not contextual_routing_enabled():
            return None
        from backend.services.delegation_policy import (
            select_specialist, specialist_inventory)
        from backend.services.contextual_policy import ContextualRoutingPolicy
        from backend.repositories.sqlite.adaptive_policy_repository import (
            get_adaptive_policy_repo)
        repo = get_adaptive_policy_repo()
        if repo is None:
            return None
        policy = ContextualRoutingPolicy(repo)
        eligible = candidates or [e["role"] for e in specialist_inventory()
                                  if e["callable"]]
        if role.value not in eligible:
            eligible = sorted(set(eligible + [role.value]))
        out = await select_specialist(
            policy, task_category="specialist_delegation",
            capability=role.value, candidates=eligible)
        from backend.services.routing_recorder import record_model_selection
        return await record_model_selection(
            run_id=parent_id, task_id=None,
            eligible_actions=eligible, selected_action=out["selected_role"],
            task_category="specialist_delegation",
            capability=role.value,
            selection_mechanism=out["mechanism"],
            selection_probability=out["selection_probability"])
    except Exception:
        return None   # selection observability never breaks delegation


async def delegate_specialist(role: AgentRole, *, parent_kind: str,
                              parent_id: str, tenant_id: str,
                              input_data: Dict[str, Any],
                              output_schema: Optional[Dict[str, Any]] = None,
                              allowed_tools: Optional[Any] = None,
                              acceptance_criteria: Optional[List[Dict[str, Any]]] = None,
                              budget_units: int = 100,
                              deadline_seconds: int = 900,
                              depth: int = 0,
                              dedup_key: Optional[str] = None,
                              completion_policy: str = "require_all",
                              repo=None) -> AgentTaskContract:
    """§15.4: delegate to a specialist with the registry's output schema
    as the default (the caller may narrow it, never widen past the role's
    boundary — the role-tool whitelist still enforces)."""
    from backend.services.agent_contracts import delegate as _delegate
    registry_entry = SPECIALIST_REGISTRY.get(role)
    if registry_entry is None:
        raise AgentTaskError(f"Unknown specialist role: {role}",
                             "UNKNOWN_ROLE")
    schema = {**registry_entry["output_schema"], **(output_schema or {})}
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(k, str) and k for k in required):
        raise AgentTaskError("Required output fields must be a list of names", "INVALID_OUTPUT_SCHEMA")
    schema["required"] = list(dict.fromkeys([*registry_entry["output_schema"]["required"], *required]))
    criteria = list(acceptance_criteria or [])
    if not criteria:
        # §15.3: the registry's default completion criteria — a
        # specialist never starts with ambiguous success.
        criteria = [dict(c) for c in registry_entry.get(
            "default_acceptance", [])]
    effective_tools = list(allowed_tools) if allowed_tools is not None else \
        sorted(ROLE_ALLOWED_TOOLS.get(role, set()))
    # §16.15 (finding G): the specialist's MEMORY SCOPE rides through
    # the delegation — the minimum relevant filters, non-widening; the
    # child cannot issue its own unrestricted search.
    input_payload = dict(input_data or {})
    from backend.services.memory_governance import specialist_memory_filters
    try:
        if "memory_scope" in input_payload and input_payload["memory_scope"] is None:
            raise ValueError("Invalid specialist memory scope")
        input_payload["memory_scope"] = specialist_memory_filters(role.value, input_payload.get("memory_scope"))
    except ValueError as exc:
        raise AgentTaskError(str(exc), "INVALID_MEMORY_SCOPE") from exc
    # §15.24: the learned-selection decision (release-scoped) rides with
    # the delegation — logged, never authoritative.
    await record_selection_for_delegation(role, parent_id=parent_id,
                                          tenant_id=tenant_id)
    return await _delegate(
        parent_kind=parent_kind, parent_id=parent_id, role=role,
        tenant_id=tenant_id, input_data=input_payload,
        output_schema=schema, allowed_tools=effective_tools,
        budget_units=budget_units, deadline_seconds=deadline_seconds,
        depth=depth, ancestor_ids=None, completion_policy=completion_policy,
        dedup_key=dedup_key, acceptance_criteria=criteria, repo=repo)
