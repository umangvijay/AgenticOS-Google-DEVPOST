"""Delegation service (Phase 15 §15.1–15.6).

The ONE entry point for creating and driving agent task contracts:
- Role authority boundaries (§15.1–15.3): a contract's allowed tools
  must be a subset of the role's whitelist — a verifier can never gain
  dispatch authority, a recovery analyst can never broaden permissions.
- §15.5 bounds enforced by the repository (depth, fan-out, concurrent
  children, cumulative spend, cycle detection).
- §15.6 partial failure: completion verdicts name what is missing —
  incomplete combined results are never silently accepted.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from backend.models.agent_task import (
    AgentRole,
    AgentTaskContract,
    AgentTaskError,
    AgentTaskState,
    ROLE_ALLOWED_TOOLS,
    validate_output_schema,
)
from backend.repositories.agent_task_repository import (
    AgentTaskRepository, get_agent_task_repo, new_contract_id,
)

logger = logging.getLogger("agent_contracts")


async def delegate(*, parent_kind: str, parent_id: str, role: AgentRole,
                   tenant_id: str, input_data: Dict[str, Any],
                   output_schema: Optional[Dict[str, Any]] = None,
                   allowed_tools: Optional[List[str]] = None,
                   budget_units: int = 100, deadline_seconds: int = 900,
                   depth: int = 0, ancestor_ids: Optional[List[str]] = None,
                   completion_policy: str = "require_all",
                   dedup_key: Optional[str] = None,
                   goal: str = "", exclusions: Optional[List[str]] = None,
                   input_refs: Optional[List[str]] = None,
                   required_capabilities: Optional[List[str]] = None,
                   acceptance_criteria: Optional[List[Dict[str, Any]]] = None,
                   retry_policy: str = "bounded",
                   agent_version: str = "", tool_version: str = "",
                   policy_version: str = "",
                   repo: Optional[AgentTaskRepository] = None
                   ) -> AgentTaskContract:
    """Create a bounded delegation. Role boundaries are deterministic: a
    request for tools OUTSIDE the role's whitelist is REFUSED, never
    silently trimmed — the caller must know the boundary was hit.
    §15.3: the contract must be COMPLETE (authority + success criteria
    unambiguous) before it can be created; §15.6: equivalent active
    work under the same parent is refused."""
    if repo is None:
        repo = get_agent_task_repo()
    if repo is None:
        raise AgentTaskError("Agent task repository not configured",
                             "REPO_UNAVAILABLE")
    # §15.30 (audit W1): the release-stage gate is ENFORCED at the
    # contract layer — a role whose stage is disabled (or that the
    # operator disabled) refuses NEW delegation; history is untouched.
    try:
        from backend.config.settings import settings as _settings
        from backend.services.specialist_release import require_role_stage
        require_role_stage(role.value, _settings)
    except Exception as exc:
        from backend.services.specialist_release import (
            SpecialistReleaseError)
        if isinstance(exc, SpecialistReleaseError):
            raise AgentTaskError(str(exc), "ROLE_STAGE_DISABLED") from exc
        raise
    permitted = ROLE_ALLOWED_TOOLS.get(role, set())
    requested = set(allowed_tools or [])
    disallowed = requested - permitted
    if disallowed:
        raise AgentTaskError(
            f"Role {role.value} cannot hold tools: "
            f"{', '.join(sorted(disallowed))} (authority boundary, §15)",
            "ROLE_TOOL_FORBIDDEN")
    from backend.models.admission import epoch_now
    from backend.models.agent_task import validate_contract_completeness
    contract = AgentTaskContract(
        contract_id=new_contract_id(),
        parent_kind=parent_kind, parent_id=parent_id,
        ancestor_ids=list(ancestor_ids or []),
        role=role, tenant_id=tenant_id,
        dedup_key=dedup_key,
        input_data=dict(input_data or {}),
        output_schema=dict(output_schema or {}),
        allowed_tools=sorted(requested & permitted),
        budget_units=int(budget_units),
        deadline_at=epoch_now() + int(deadline_seconds),
        depth=int(depth),
        completion_policy=completion_policy,
        goal=str(goal or ""),
        exclusions=list(exclusions or []),
        input_refs=list(input_refs or []),
        required_capabilities=list(required_capabilities or []),
        acceptance_criteria=list(acceptance_criteria or []),
        retry_policy=retry_policy,
        agent_version=agent_version, tool_version=tool_version,
        policy_version=policy_version,
    )
    # §15.3: no specialist starts with ambiguous authority or success.
    validate_contract_completeness(contract)
    # §15.6: equivalent ACTIVE work under the same parent is a cycle
    # signal — refused with the existing contract named.
    import hashlib as _hl
    fingerprint = _hl.sha256(
        (contract.goal + "|" + contract.role.value).encode()).hexdigest()[:16]
    existing = await repo.find_equivalent_active(parent_id, fingerprint)
    if existing is not None:
        raise AgentTaskError(
            f"Equivalent work already delegated: {existing.contract_id} "
            f"({contract.role.value}) — repeated delegation of the same "
            "goal is a cycle signal", "EQUIVALENT_WORK_ACTIVE")
    return await repo.create_contract(contract)


async def claim_specialist(repo, contract_id: str, role: str) -> str:
    actor = f"{role}-{uuid.uuid4().hex}"
    if await repo.claim(contract_id, actor) is None:
        raise AgentTaskError("Specialist contract could not be claimed", "CLAIM_FAILED")
    return actor


async def complete(contract_id: str, result: Dict[str, Any], *,
                   spent_units: int = 0,
                   actor: Optional[str] = None,
                   repo: Optional[AgentTaskRepository] = None) -> AgentTaskContract:
    """Complete a contract — the result is validated against the typed
    output schema BEFORE the state moves (§15.4 typed outputs), and the
    completion is FENCED on the current claim (§15.9): the actor must
    name the claim it holds. The inline path (completing an unclaimed
    contract) claims as 'inline' under THIS call's authority."""
    repo = repo or get_agent_task_repo()
    if repo is None:
        raise AgentTaskError("Agent task repository not configured",
                             "REPO_UNAVAILABLE")
    contract = await repo.get_contract(contract_id)
    if contract is None:
        raise AgentTaskError("Contract not found", "NOT_FOUND")
    if contract.state is AgentTaskState.PROPOSED:
        # Completing without ever running is not a valid claim: the
        # assignment/running steps are skipped only for instant roles.
        contract = await repo.claim(contract_id, "inline", lease_seconds=60)
        if contract is None:
            raise AgentTaskError("Contract could not be claimed",
                                 "CLAIM_FAILED")
        actor = "inline"
    elif actor is None:
        raise AgentTaskError(
            "actor is required to complete a claimed contract (§15.9 "
            "fencing)", "CLAIM_ACTOR_REQUIRED")
    return await repo.transition(contract_id, AgentTaskState.COMPLETED,
                                 actor=actor or "inline",
                                 result=result, spent_units=spent_units)


async def fail(contract_id: str, error: str, *,
               actor: Optional[str] = None,
               repo: Optional[AgentTaskRepository] = None) -> AgentTaskContract:
    """§15.9: failure is fenced like completion — the current claim
    owner (or the inline claimant) names itself."""
    repo = repo or get_agent_task_repo()
    if actor is None:
        contract = await repo.get_contract(contract_id) \
            if repo else None
        if contract is None:
            raise AgentTaskError("Contract not found", "NOT_FOUND")
        if contract.state is AgentTaskState.PROPOSED:
            actor = "inline"
        else:
            raise AgentTaskError(
                "actor is required to fail a claimed contract (§15.9)",
                "CLAIM_ACTOR_REQUIRED")
    return await repo.transition(contract_id, AgentTaskState.FAILED,
                                 actor=actor, error=error)


async def cancel(contract_id: str, *, actor: str = "operator",
                 repo: Optional[AgentTaskRepository] = None) -> Dict[str, Any]:
    """Cancel a contract and propagate to its active children (§15.4)."""
    repo = repo or get_agent_task_repo()
    if hasattr(repo, "cancel_tree"):
        return await repo.cancel_tree(contract_id, actor=actor)
    contract = await repo.transition(contract_id, AgentTaskState.CANCELLED,
                                     actor=actor,
                                     error=f"cancelled by {actor}")
    propagated = await repo.cancel_subtree(contract_id, actor=actor)
    return {"contract_id": contract_id, "state": contract.state.value,
            "children_cancelled": propagated}
