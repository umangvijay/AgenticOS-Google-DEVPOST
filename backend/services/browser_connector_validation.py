"""Validate the fixed public page read independently of generated browser actions."""
import asyncio
import json
import time
import uuid
from urllib.parse import urlsplit

from backend.services.tool_validation import definition_digest, credential_binding_id_from

PROFILE = "browser.navigation.v1"


def _validated_start(tool, manifest):
    op = tool.get("operation") or {}
    if manifest.get("source_type") != "website" or op.get("kind") != "browser":
        raise ValueError("Not a website browser adapter")
    if op.get("action") != "read_start_page":
        raise ValueError("This navigation test covers reading the public start page only")
    if tool.get("risk_level") != 3:
        raise ValueError("Browser tools must retain HIGH-risk action approvals")
    origin = urlsplit(op.get("origin") or "")
    start = urlsplit(op.get("start_url") or "")
    source = urlsplit(manifest.get("source_uri") or "")
    for target in (origin, start, source):
        if target.scheme != "https" or not target.hostname or target.username is not None or target.password is not None:
            raise ValueError("Browser tools require a public HTTPS start page")
    def key(u):
        return u.scheme, u.hostname, u.port or 443
    if key(origin) != key(start) or key(origin) != key(source):
        raise ValueError("Browser start page must match the connector's exact origin")
    return op["start_url"]


def has_valid_browser_probe(tool, manifest):
    try:
        _validated_start(tool, manifest)
        evidence = (tool.get("operation") or {}).get("_validation") or {}
        auth = manifest.get("auth") or {}
        if isinstance(auth, str):
            auth = json.loads(auth)
        return (manifest.get("trust_tier") == "browser_ready"
                and bool(manifest.get("active_revision_id"))
                and evidence.get("revision_id") == manifest["active_revision_id"]
                and evidence.get("kind") == PROFILE and evidence.get("status") == "passed"
                and evidence.get("digest") == definition_digest(tool, manifest.get("spec_hash"))
                and credential_binding_id_from(auth) is not None
                and evidence.get("credential_binding_id") == credential_binding_id_from(auth)
                and float(evidence.get("valid_until") or 0) > time.time())
    except (ValueError, TypeError, KeyError):
        return False


async def probe_start_page(url, user_id):
    from backend.models.admission import ResourceClass
    from backend.services.capacity_service import capacity_slot
    from backend.services.website_health import check_website

    async with capacity_slot(ResourceClass.PROVIDER_CALL, scope_key=f"user:{user_id}",
                             owner_id=user_id, execution_key=f"page-read:{uuid.uuid4()}", ttl_seconds=90):
        async with asyncio.timeout(45):
            page = await check_website(url, same_origin_only=True)
    if page["error"]:
        raise ValueError(f"Could not read the public start page: {page['error']}")
    if not page["status_code"] or not 200 <= page["status_code"] < 300:
        raise ValueError(f"Could not read the public start page (HTTP {page['status_code']})")
    return {"url": page["final_url"], "http_status": page["status_code"], "title": page["title"]}



async def validate_browser_connector(repo, manifest, tools, user_id):
    """Commit evidence for the fixed public read on this revision."""
    from backend.services.connector_revisions import checkpoint, canonical_definition_digest
    initial_digest = canonical_definition_digest(manifest, tools)
    # Older website builds contain only generated, free-form actions. Add a
    # distinct fixed read whose invocation exactly matches the live test.
    fixed = next((t for t in tools if t.get("operation", {}).get("action") == "read_start_page"), None)
    if fixed is None:
        if not tools:
            raise ValueError("No website tools registered")
        existing = {t["tool_name"] for t in tools}
        name = "readStartPage"
        suffix = 1
        while name in existing:
            suffix += 1
            name = f"readStartPage{suffix}"
        start = manifest["source_uri"]
        fixed = {**tools[0], "tool_name": name, "description": "Read the title and status of the public start page.",
                 "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                 "operation": {"kind": "browser", "action": "read_start_page", "origin": start, "start_url": start}}
    other_tools = [t for t in tools if t["tool_name"] != fixed["tool_name"]]
    start = _validated_start(fixed, manifest)
    original_binding = credential_binding_id_from(manifest.get("auth") or {})
    observation = await probe_start_page(start, user_id)
    fresh = await repo.get_mcp(manifest["mcp_id"])
    current_tools = await repo.get_cached_tools(manifest["mcp_id"])
    if (not fresh or canonical_definition_digest(fresh, current_tools) != initial_digest
            or credential_binding_id_from(fresh.get("auth") or {}) != original_binding
            or fresh.get("is_enabled") != manifest.get("is_enabled")):
        raise ValueError("Connector changed during browser validation. Run Test again.")
    now = int(time.time())
    binding = original_binding
    revision_id = canonical_definition_digest(manifest, other_tools + [fixed])
    evidence = {
        "revision_id": revision_id,
        "kind": PROFILE, "status": "passed", "provenance": "inferred_description",
        "digest": definition_digest(fixed, manifest.get("spec_hash")),
        "credential_binding_id": binding, "valid_until": now + 86400,
        "scope": "public start page title and status", **observation,
    }
    tested = {**fixed, "operation": {**fixed["operation"], "_validation": evidence}}
    await repo.cache_tools(manifest["mcp_id"], [tested], replace_all=False)
    revision = await checkpoint(repo, manifest["mcp_id"])
    if revision["revision_id"] != revision_id:
        raise ValueError("Connector changed during browser validation. Run Test again.")
    await repo.save_validation_run(manifest["mcp_id"], {
        "validation_run_id": str(uuid.uuid4()), "revision_id": revision["revision_id"],
        "operation_id": tested["tool_name"], "operation_digest": evidence["digest"],
        "credential_binding_id": binding, "validation_profile_version": PROFILE,
        "status": "passed", "started_at": now, "completed_at": now,
        "valid_until": evidence["valid_until"], "case_results": [evidence],
    })
    from backend.services.connector_activation import activate_revision
    await activate_revision(repo, mcp_id=manifest["mcp_id"], candidate_revision_id=revision["revision_id"],
                            expected_current_revision_id=manifest.get("active_revision_id") or "",
                            eligibility_allowed=True, actor="browser-navigation-validator", browser_ready=True)
    return {"status": "browser_ready", "tool_name": fixed["tool_name"], "tools_discovered": len(other_tools) + 1,
            "validated_tool_count": 1, "probe": {"scope": "start-page navigation", "pages": [observation]},
            "message": "The public start page opened successfully. Other proposed actions need separate validation."}
