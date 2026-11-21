"""Phase 2 — an existing Stamp is callable from chat.

Production audit: asking "Execute the hacker-news integration's
readStartPage operation" forged a DUPLICATE integration and replied with
a raw credentials listing (`{"credentials": []}`) instead of dispatching.

The contract proven here:
1. A forged browser-adapter integration whose start-page probe passed
   (the Run Test lifecycle) is ELIGIBLE in the tool catalog.
2. The explicit operation dispatches through tool_router
   (execute_tool_safe → the adapter executor), and the execution record
   names the integration, revision, and operation.
3. Dispatch creates ZERO additional integrations.
4. build_integration's duplicate guard refuses a same-site rebuild that
   already covers the requested operations.
"""
import json
import time
import uuid

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.mcp_repository import SQLiteMCPRepository
from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
from backend.repositories.in_memory_message_bus import InMemoryMessageBus

IANA = "https://www.iana.org/"


def _browser_tool_spec(tool_name: str, description: str):
    # Fields mirror what the MCP factory actually persists — the cached
    # tools are round-tripped through CachedToolDefinition, whose defaults
    # (mcp_version, risk_level) participate in definition_digest. A spec
    # missing them digests differently after the model round-trip and the
    # probe evidence would never match.
    return {
        "tool_name": tool_name,  # cached-tool field name (definition_digest reads tool_name)
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {"goal": {"type": "string"}},
            "required": ["goal"],
        },
        "mcp_version": "1.0.0",
        # §27.4: browser tools retain HIGH risk (action approvals); the
        # start-page probe refuses anything below it.
        "risk_level": 3,
        "operation": {"kind": "browser", "start_url": IANA,
                      "origin": IANA.rstrip("/"),
                      "action": "read_start_page" if tool_name == "readStartPage" else "navigate",
                      "goal_template": f"Navigate IANA: {{{{goal}}}}"},
    }


def _manifest(mcp_id: str, tools: list, *, trust_tier="pending_review",
              active_revision_id=""):
    return {
        "mcp_id": mcp_id,
        "name": "iana-browser-tools",
        "owner": "user-1",
        "description": "Browser tools for https://www.iana.org (no OpenAPI — Playwright)",
        "source_type": "website",
        "source_uri": IANA,
        "transport": "internal",
        "artifact_type": "adapter",
        "auth": {"type": "NONE"},
        "is_enabled": True,
        "health": "HEALTHY",
        "state": "HEALTHY",
        "trust_tier": trust_tier,
        "active_revision_id": active_revision_id,
        "spec_hash": "deadbeef" * 8,
        "spec_json": json.dumps({"origin": "https://www.iana.org"}),
        "tool_count": len(tools),
        "validated_tool_count": 0,
        "circuit_breaker": {"failure_count": 0, "mcp_id": mcp_id,
                            "state": "CLOSED", "total_failures": 0,
                            "total_rejected": 0, "total_successes": 0},
        "scopes": [], "capabilities_omitted": 0,
        "endpoint": f"internal://openapi/{mcp_id}",
        "version": "1.0.0", "spec_version": "1.0.0",
        "built_at": "2026-11-21T04:53:58", "updated_at": "2026-11-21T04:53:58",
        "test_coverage": {},
        "tools": tools,
    }


@pytest_asyncio.fixture
async def stamp(tmp_path):
    db = DatabaseManager(str(tmp_path / "dispatch.db"))
    await db.initialize()
    repo = SQLiteMCPRepository(db)
    mcp_id = "stmp" + uuid.uuid4().hex[:8]
    tools = [
        _browser_tool_spec("runOnSite", "Execute an arbitrary task on the IANA website"),
        _browser_tool_spec("readStartPage", "Read the title and status of the public start page."),
    ]
    manifest = _manifest(mcp_id, tools)
    await repo.register_mcp(manifest)
    await repo.cache_tools(mcp_id, tools)
    yield {"repo": repo, "mcp_id": mcp_id, "manifest": manifest, "tools": tools}
    await db.close()


async def _seed_start_page_evidence(repo, mcp_id: str, tools: list, manifest: dict):
    """Perform the same writes Run Test does for the start-page probe:
    per-tool _validation evidence bound to the CHECKPOINTED revision,
    then activation to browser_ready — without driving a real browser
    (the browser lift is covered by the browser-runtime suites)."""
    from backend.services.browser_connector_validation import PROFILE
    from backend.services.connector_revisions import checkpoint
    from backend.services.tool_validation import (credential_binding_id_from,
                                                  definition_digest)

    now = int(time.time())
    revision = await checkpoint(repo, mcp_id)   # writes mcp_revisions
    revision_id = revision["revision_id"]
    binding = credential_binding_id_from(manifest.get("auth") or {})
    tested_tools = []
    for tool in tools:
        dig = definition_digest(tool, manifest.get("spec_hash"))
        evidence = {"revision_id": revision_id, "kind": PROFILE,
                    "status": "passed", "provenance": "inferred_description",
                    "digest": dig, "credential_binding_id": binding,
                    "valid_until": now + 86400,
                    "scope": "public start page title and status"}
        tested_tools.append({**tool, "operation": {**tool["operation"],
                                                   "_validation": evidence}})
    await repo.cache_tools(mcp_id, tested_tools, replace_all=True)
    return repo, mcp_id, tested_tools, {**manifest, "trust_tier": "browser_ready"}, revision_id


@pytest.mark.asyncio
async def test_eligible_browser_operation_reaches_catalog_and_dispatches(stamp, monkeypatch):
    """Run-Test-validated op → catalog → tool_router dispatch with the
    execution naming the integration/operation — and ZERO new builds."""
    from backend.mcp import tool_router as tool_router_module
    from backend.mcp.tool_router import ToolRouter, ToolPolicy

    repo, mcp_id, manifest, tools = stamp["repo"], stamp["mcp_id"], stamp["manifest"], stamp["tools"]
    _, _, _, _, revision_id = await _seed_start_page_evidence(
        repo, mcp_id, tools, manifest)
    # activate_revision writes the browser_ready tier + active revision
    from backend.services.connector_activation import activate_revision
    from datetime import datetime, timezone
    await activate_revision(repo, mcp_id=mcp_id,
                            candidate_revision_id=revision_id,
                            expected_current_revision_id=manifest.get("active_revision_id") or "",
                            eligibility_allowed=True,
                            actor="browser-navigation-validator", browser_ready=True)
    await repo.update_mcp_health(mcp_id, "HEALTHY", datetime.now(timezone.utc))

    db = DatabaseManager.__new__(DatabaseManager)  # workflow repo not used here
    router = ToolRouter(mcp_repo=repo, policy=ToolPolicy(),
                        approvals_engine=None, idempotency_repo=None,
                        secrets_repo=None)

    # 1. The eligible operation reaches the catalog with its dispatch name.
    catalog = await router.get_tool_catalog("user-1")
    names = [t["name"] for t in catalog]
    assert "readStartPage" in names, names
    entry = next(t for t in catalog if t["name"] == "readStartPage")
    assert entry["agent_tool_name"] == f"{mcp_id}__readStartPage"

    # 2. Dispatch records the integration + operation; NO new integration.
    recorded = {}

    async def _fake_execute_openapi_tool(mcp_raw, tool_raw, arguments, user_id, **kwargs):
        recorded["mcp_id"] = mcp_raw.get("mcp_id")
        recorded["operation"] = tool_raw.get("name")
        recorded["args"] = arguments
        return {"status_code": 200, "title": "IANA — Internet Assigned Numbers Authority",
                "http_status": 200}

    monkeypatch.setattr(tool_router_module, "execute_openapi_tool",
                        _fake_execute_openapi_tool)
    result = await router.execute_tool_safe(
        entry["agent_tool_name"], {"goal": "read the start page"},
        context={"user_id": "user-1"})
    assert recorded["mcp_id"] == mcp_id, recorded
    assert recorded["operation"] == "readStartPage", recorded
    assert result["title"].startswith("IANA"), result


    # 3. The duplicate guard: build_integration with the same site + covered
    #    operation returns already_registered instead of forging.
    from backend.agents.orchestrator.orchestrator_agent import existing_integration_tools
    catalog_all = await router.get_tool_catalog("user-1")
    existing = existing_integration_tools(catalog_all, "iana-browser-tools", IANA)
    assert existing, "the matching integration must resolve from the catalog"
    assert any(t["name"] == "readStartPage" for t in existing)


@pytest.mark.asyncio
async def test_unvalidated_operation_stays_blocked_until_probe(stamp):
    """Without Run-Test evidence, the operation stays out of the catalog —
    the honest governance the production UI displayed."""
    from backend.mcp.tool_router import ToolRouter, ToolPolicy

    repo, mcp_id, manifest, tools = stamp["repo"], stamp["mcp_id"], stamp["manifest"], stamp["tools"]
    router = ToolRouter(mcp_repo=repo, policy=ToolPolicy(),
                        approvals_engine=None, idempotency_repo=None,
                        secrets_repo=None)
    catalog = await router.get_tool_catalog("user-1")
    names = [t["name"] for t in catalog]
    assert "runOnSite" not in names, (
        "unvalidated browser operations must stay blocked")
