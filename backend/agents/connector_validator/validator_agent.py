"""Connector Validator agent (Phase 15 §15.2) — proposes operation-
specific test cases through the bounded contract system. The §6
validation pipeline executes the cases and records the actual outcomes;
the agent cannot invent passing results.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.agents.verifier.verifier_agent import (
    ValidationContext, ValidationTestPlan, VALIDATOR_OUTPUT_SCHEMA,
    get_validator_agent,
)

logger = logging.getLogger(__name__)

__all__ = ["ValidationContext", "ValidationTestPlan", "get_validator_agent",
           "run_validator_via_contract", "VALIDATOR_OUTPUT_SCHEMA"]


async def run_validator_via_contract(contract_repo, ctx: ValidationContext,
                                     *, model: Optional[str] = None,
                                     judge_client: Optional[Any] = None,
                                     parent_id: str = "engine",
                                     principal_id: Optional[str] = None,
                                     org_id: Optional[str] = None,
                                     depth: int = 0,
                                     authorization_context: Optional[Dict[str, Any]] = None,
                                     ) -> Dict[str, Any]:
    """Delegate test-case PROPOSAL through a bounded contract. The
    proposed cases feed the §6 validation pipeline (which executes and
    records them); the contract completes with the proposal as its typed
    output."""
    from backend.services.agent_authority import (
        require_specialist_access, resolve_principal_from_parent)
    from backend.services.agent_contracts import claim_specialist, delegate, complete, fail
    from backend.models.agent_task import AgentRole
    # §27.2: a run ID identifies WORK, never an actor. The principal is
    # the authenticated caller or the parent run's PERSISTED owner;
    # unresolved authority fails closed (no silent run-ID principal).
    principal = principal_id
    if not principal:
        principal = await resolve_principal_from_parent("run", parent_id)
    if not principal:
        from backend.services.agent_contracts import AgentTaskError
        raise AgentTaskError(
            f"protected validation cannot resolve an actor: no "
            f"authenticated principal and parent run {parent_id} has no "
            "persisted owner", "AUTHORITY_UNRESOLVED")
    await require_specialist_access(principal, authorization_context, org_id)
    contract = await delegate(
        parent_kind="run", parent_id=parent_id,
        role=AgentRole.CONNECTOR_VALIDATOR,
        tenant_id=principal, input_data=ctx.model_dump(),
        output_schema=VALIDATOR_OUTPUT_SCHEMA,
        allowed_tools=["connector.read", "connector.test", "evidence.read"],
        acceptance_criteria=[{"kind": "non_empty",
                              "description": "a test-case proposal for the "
                                             "exact operation exists"}],
        dedup_key=f"validate:{ctx.connector_id}:{ctx.operation_id}",
        depth=depth, repo=contract_repo)
    actor = await claim_specialist(contract_repo, contract.contract_id, "validator")
    try:
        if judge_client is None:
            # No model configured: propose the DETERMINISTIC baseline case
            # set (honest — never fabricated model output).
            plan = {"operation_id": ctx.operation_id,
                    "test_cases": _baseline_cases(ctx),
                    "risks": ["deterministic baseline — no model proposer "
                              "configured"]}
        elif judge_client is not None:
            # The injected proposer (tests / alternate backends).
            import inspect
            sig = inspect.signature(judge_client)
            raw = (await judge_client(ctx.model_dump_json()[:8000])
                   if len(sig.parameters) >= 1 else await judge_client())
            parsed: Dict[str, Any] = _parse_agent_output(raw)
            plan = parsed or {"operation_id": ctx.operation_id,
                              "test_cases": _baseline_cases(ctx),
                              "risks": ["model proposer returned nothing — "
                                        "deterministic baseline used"]}
        await require_specialist_access(principal, authorization_context, org_id)
        done = await complete(contract.contract_id, plan,
                              actor=actor,
                              repo=contract_repo)
        return {"contract_id": done.contract_id, "plan": plan}
    except Exception as exc:
        await fail(contract.contract_id, str(exc),
                   actor=actor, repo=contract_repo)
        raise


def _parse_agent_output(raw: Any) -> Dict[str, Any]:
    """Normalize an injected judge's return: dict | ADK-style events
    (objects with .output) | pydantic models | a bare value."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and raw:
        first = raw[0]
        if hasattr(first, "output"):
            out = first.output
            if isinstance(out, dict):
                return out
            if hasattr(out, "model_dump"):
                return out.model_dump()
            return {}
        if hasattr(first, "model_dump"):
            return first.model_dump()
        return first if isinstance(first, dict) else {}
    if raw is not None and hasattr(raw, "output"):
        out = raw.output
        if isinstance(out, dict):
            return out
        if hasattr(out, "model_dump"):
            return out.model_dump()
    if raw is not None and hasattr(raw, "model_dump"):
        return raw.model_dump()
    return {}


def _baseline_cases(ctx: ValidationContext) -> List[Dict[str, Any]]:
    """The deterministic case set (§6 test pack) — used verbatim when no
    model proposer is configured."""
    cases = [
        {"name": "auth_denial", "kind": "auth_denial",
         "request_hint": "call without valid credentials",
         "expected_observation": "401/403 — never a false success"},
        {"name": "required_params", "kind": "required_params",
         "request_hint": "omit each required parameter in turn",
         "expected_observation": "422/400 per missing param"},
        {"name": "malformed_response", "kind": "malformed_response",
         "request_hint": "controlled malformed payload",
         "expected_observation": "structured error, no crash"},
    ]
    schema = ctx.operation_schema or {}
    if schema.get("paginated") or "list" in ctx.operation_id.lower():
        cases.append({"name": "pagination", "kind": "pagination",
                      "request_hint": "page through with small page size",
                      "expected_observation": "stable ordering, no dupes"})
    if ctx.operation_schema.get("writes") or "create" in ctx.operation_id.lower() \
            or "update" in ctx.operation_id.lower():
        cases.append({"name": "controlled_write", "kind": "controlled_write",
                      "request_hint": "write to the controlled test account",
                      "expected_observation": "record created + read-back "
                                              "confirms"})
    # §15.19: the full operational case set — the validator is USEFUL,
    # not decorative.
    cases.append({"name": "empty_response", "kind": "empty_response",
                  "request_hint": "query that matches nothing",
                  "expected_observation": "empty-but-valid response, "
                                          "no crash"})
    cases.append({"name": "schema_mismatch", "kind": "schema_mismatch",
                  "request_hint": "response with a missing/renamed field",
                  "expected_observation": "structured mismatch error, "
                                          "no silent None"})
    cases.append({"name": "rate_limit_timeout", "kind": "rate_limit_timeout",
                  "request_hint": "throttled request + slow endpoint",
                  "expected_observation": "429/backoff honored; timeout "
                                          "is an error, not a hang"})
    cases.append({"name": "credential_revision_change",
                  "kind": "credential_revision_change",
                  "request_hint": "rotate credential / bump revision",
                  "expected_observation": "stale evidence superseded; "
                                          "re-validation required"})
    return cases
