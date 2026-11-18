"""Retain the factory's observed anonymous read probe and activate that revision.

Only the actual probed operation gains evidence. No write, authenticated call,
standalone protocol or unprobed operation is certified by this path.
"""
from backend.services.connector_revisions import checkpoint, canonical_definition_digest
from backend.services.connector_validation_service import start_validation_run, complete_validation_run
from backend.services.connector_activation import activate_revision
from backend.services.tool_validation import credential_binding_id_from, has_valid_read_probe, definition_digest


async def persist_build_probe(repo, mcp_id, tool, observed, *,
                              owner_user_id=None, parent_run_id=None):
    manifest = await repo.get_mcp(mcp_id)
    binding = credential_binding_id_from(manifest.get("auth") or {})
    if observed.get("ok") is not True or not has_valid_read_probe(tool, manifest.get("spec_hash"), binding):
        raise ValueError("Cannot activate without the observed context-bound read probe")
    revision = await checkpoint(repo, mcp_id)
    # §27.5 (audit 2026-11-13): the ORDINARY build path routes the
    # operation's test-case proposal through the CONNECTOR_VALIDATOR
    # specialist contract (real delegation, typed output). The
    # INDEPENDENT anonymous read probe stays the activation evidence —
    # a proposal can never substitute for the probe, and a delegation
    # that cannot run (no contract repo, unresolvable actor) degrades
    # honestly instead of blocking or fabricating coverage.
    proposal_summary = None
    try:
        from backend.repositories.agent_task_repository import get_agent_task_repo
        from backend.agents.connector_validator.validator_agent import (
            ValidationContext, run_validator_via_contract)
        if get_agent_task_repo() is not None and (owner_user_id or parent_run_id):
            ctx = ValidationContext(
                connector_id=mcp_id, revision_id=revision["revision_id"],
                operation_id=str((tool.get("operation") or {}).get("operation_id")
                                 or tool.get("tool_name") or "unknown"),
                operation_schema=tool.get("input_schema") or {},
                auth_method=str((manifest.get("auth") or {}).get("type")
                                or "api_key"),
                existing_coverage=["anonymous_read_probe"])
            proposal = await run_validator_via_contract(
                get_agent_task_repo(), ctx,
                parent_id=parent_run_id or f"build:{mcp_id}",
                principal_id=owner_user_id)
            proposal_summary = {
                "contract_id": proposal.get("contract_id"),
                "cases_proposed":
                    len(proposal.get("test_cases") or []),
                "proposed_by": "connector_validator_contract",
            }
    except Exception as exc:   # never blocks the probe-evidenced build
        proposal_summary = {"proposal_unavailable": str(exc)[:160]}
    run = await start_validation_run(repo, connector_id=mcp_id,
        mcp={**manifest, "active_revision_id": revision["revision_id"]}, tool=tool,
        credential_binding_id=binding, environment="build-probe", valid_hours=24)
    fresh = await repo.get_mcp(mcp_id)
    tools = await repo.get_cached_tools(mcp_id)
    current_tool = next((item for item in tools if item.get("tool_name") == tool["tool_name"]), None)
    if current_tool is None:
        raise ValueError("Probed tool disappeared during registration")
    case_results = [{"case": "anonymous_read_probe", "status": "passed",
                     "http_status": observed.get("http_status")}]
    if proposal_summary is not None:
        case_results.append({"case": "validator_case_proposal",
                             "status": "consumed",
                             **proposal_summary})
    completed = await complete_validation_run(repo, run=run, passed=True,
        case_results=case_results,
        current_definition_digest=definition_digest(current_tool, fresh.get("spec_hash")),
        current_credential_binding_id=credential_binding_id_from(fresh.get("auth") or {}))
    if completed.status != "passed" or canonical_definition_digest(fresh, tools) != revision["revision_id"]:
        raise ValueError("Connector changed during build validation")
    return await activate_revision(repo, mcp_id=mcp_id, candidate_revision_id=revision["revision_id"],
        expected_current_revision_id=manifest.get("active_revision_id") or "", eligibility_allowed=True, actor="build-validator")
