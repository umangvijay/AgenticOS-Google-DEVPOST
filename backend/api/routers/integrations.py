"""
AgentOS — Integrations Router

GET  /api/v1/integrations                — List all integrations
POST /api/v1/integrations/build          — Build from OpenAPI spec
POST /api/v1/integrations/build-from-url — Build from URL
POST /api/v1/integrations/build-from-prompt — Build from description
GET  /api/v1/integrations/{id}           — Get integration detail
POST /api/v1/integrations/{id}/test      — Test an integration
GET  /api/v1/integrations/{id}/health    — Get health status
POST /api/v1/integrations/{id}/enable    — Enable
POST /api/v1/integrations/{id}/disable   — Disable
DELETE /api/v1/integrations/{id}         — Delete
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends, status
from pydantic import BaseModel, Field

from backend.security.json_safety import safe_json_dumps
from backend.api.dependencies.auth import get_current_user, AuthenticatedUser, require_not_viewer
from backend.security.rate_limiter import check_rate_limit, check_shared_rate_limit
from backend.security.input_sanitizer import sanitize_text, InputValidationError
from backend.mcp.circuit_breaker import circuit_breaker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])


def _get_factory(request: Request):
    factory = getattr(request.app.state, "factory", None)
    if not factory:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return factory


# ── Request Models ────────────────────────────────────────────────

class BuildFromSpecRequest(BaseModel):
    spec: str           # OpenAPI/Swagger JSON/YAML
    name: Optional[str] = None

class BuildFromURLRequest(BaseModel):
    url: str            # URL to API docs or a website
    name: Optional[str] = None
    method: Optional[str] = None  # url | website

class BuildFromPromptRequest(BaseModel):
    prompt: str         # Natural language description
    name: Optional[str] = None

class BuildFromWebsiteRequest(BaseModel):
    url: str
    name: Optional[str] = None
    notes: Optional[str] = None


class BuildPythonToolsRequest(BaseModel):
    module: str = Field(min_length=1, max_length=100_000)
    tools: list[dict] = Field(min_length=1, max_length=20)
    validation: dict
    read_grants: list[str] = Field(default_factory=list, max_length=10)
    name: str = Field(default="Generated MCP", max_length=120)


def _validated_operation_count(mcp, tools):
    from backend.services.tool_validation import has_valid_read_probe, credential_binding_id_from, validation_provenance
    auth = mcp.get("auth") or {}
    if isinstance(auth, str):
        import json
        auth = json.loads(auth)
    binding = credential_binding_id_from(auth)
    return sum(has_valid_read_probe(t, mcp.get("spec_hash"), binding) for t in tools)


# ══════════════════════════════════════════════════════════════════
#  LIST / GET
# ══════════════════════════════════════════════════════════════════

@router.get("")
async def list_integrations(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List all integrations visible to the user."""
    factory = _get_factory(request)
    # Admins administer the whole platform: see every integration.
    mcps = await factory.mcp_repo.list_mcps(user_id=None if user.is_admin() else user.user_id)

    # Enrich with circuit breaker status
    results = []
    for mcp in mcps:
        mcp_id = mcp.get("mcp_id", "")
        breaker_status = circuit_breaker.get_status(mcp_id)
        mcp["circuit_breaker"] = breaker_status
        # Get cached tool count
        tools = await factory.mcp_repo.get_cached_tools(mcp_id=mcp_id)
        mcp["tool_count"] = len(tools)
        mcp["validated_tool_count"] = _validated_operation_count(mcp, tools)
        results.append(mcp)

    return {"integrations": results, "count": len(results)}


@router.get("/catalog")
async def list_catalog(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    """The shared integration catalog — every published integration."""
    factory = _get_factory(request)
    public = await factory.mcp_repo.list_public_mcps()
    out = []
    for mcp in public:
        tools = await factory.mcp_repo.get_cached_tools(mcp.get("mcp_id"))
        out.append(_catalog_entry(mcp, tools))
    return {"integrations": out, "count": len(out)}


@router.get("/{mcp_id}")
async def get_integration(
    mcp_id: str, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Get integration detail with tools and health."""
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or (mcp.get("owner") not in (user.user_id, "system") and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Integration not found")

    tools = await factory.mcp_repo.get_cached_tools(mcp_id=mcp_id)
    breaker_status = circuit_breaker.get_status(mcp_id)

    # Phase 6 §6.12: operation-level eligibility from the ONE authoritative
    # evaluator — the UI shows per-operation truth, not a broad green badge.
    from backend.services.connector_eligibility import evaluate_operation_eligibility
    from backend.services.tool_validation import (credential_binding_id_from,
                                                  validation_provenance)
    mcp_auth = mcp.get("auth") or {}
    if isinstance(mcp_auth, str):
        try:
            import json as _json
            mcp_auth = _json.loads(mcp_auth)
        except ValueError:
            mcp_auth = {}
    binding = credential_binding_id_from(mcp_auth)
    eligibility = []
    for t in tools:
        result = evaluate_operation_eligibility(
            t, mcp, auth_blob=mcp_auth,
            revision_id=mcp.get("active_revision_id"))
        eligibility.append({
            "tool_name": t.get("tool_name"),
            "allowed": result.allowed,
            "reason_codes": result.reason_codes,
            "provenance": validation_provenance(t),
            "operation_digest": result.operation_digest[:12],
        })
    return {
        **mcp,
        "tools": tools,
        "operation_eligibility": eligibility,
        "tool_count": len(tools),
        "validated_tool_count": _validated_operation_count(mcp, tools),
        # Live evidence is per-credential: the stored build-time coverage is
        # the builder's record; this count is what policy enforces now.
        "spec_provenance": mcp.get("spec_provenance", "unknown"),
        "test_coverage": mcp.get("test_coverage") or {},
        "capabilities_omitted": int(mcp.get("capabilities_omitted") or 0),
        "circuit_breaker": breaker_status,
    }


# ══════════════════════════════════════════════════════════════════
#  BUILD (MCP Factory) — async jobs with persisted stages
# ══════════════════════════════════════════════════════════════════

async def _start_build(factory, user_id: str, method: str, source: str, name: Optional[str], workflow_engine=None,
                       required_operations=None) -> dict:
    """Persist MCP build as a normal workflow task so worker restarts cannot lose it.
    Routed through the shared durable build service (audit G07)."""
    from backend.services.connector_build_service import (
        ORIGIN_API, BuildAdmissionDenied, request_durable_build,
    )

    # Phase 3 §3.10/§3.12: admission lives in the SERVICE funnel so the
    # orchestrator surface enforces the same gate; this router only maps the
    # typed denial to HTTP (§3.8 contract: 429 quota / 409 duplicate).
    try:
        result = await request_durable_build(
            factory, user_id=user_id, method=method, source=source, name=name,
            origin=ORIGIN_API, workflow_engine=workflow_engine,
            required_operations=required_operations,
        )
    except BuildAdmissionDenied as denied:
        status_code = 409 if denied.first_command_id else 429
        raise HTTPException(status_code=status_code, detail={
            "code": denied.reason.value,
            "message": denied.message,
            "retry_after_seconds": denied.retry_after_seconds,
            "accepted": False,
            "decision_id": denied.decision_id,
            **({"command_id": denied.first_command_id} if denied.first_command_id else {}),
        })
    return {"message": "MCP Factory build started. Poll /integrations/builds/{build_id} for progress.", **result}


# ── Declared supported-app catalog (§27.6) ────────────────────────

@router.get("/apps/catalog")
async def declared_app_catalog(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The DECLARED supported-application catalog: stable app/version
    IDs, publisher, auth method, documented vs tested operations and an
    honest qualification status. A declaration is NOT execution proof —
    per-operation readiness comes from each install's build records."""
    from backend.services.app_catalog import catalog_public_view
    return {"apps": catalog_public_view()}


class InstallAppRequest(BaseModel):
    base_url: Optional[str] = Field(default=None, max_length=2000)


@router.post("/apps/{app_id}/install", status_code=status.HTTP_202_ACCEPTED)
async def install_declared_app(
    app_id: str, request: Request,
    body: InstallAppRequest = None,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Install a DECLARED supported app: one durable build (idempotent
    per user+app+version) producing THIS user's own integration. The
    owner's credentials are never shared; tested operations are the
    build's required operations — documented-but-untested operations
    gain no validation evidence from this path."""
    from backend.services.app_catalog import app_by_id, install_source
    from backend.services.connector_build_service import (
        ORIGIN_API, BuildAdmissionDenied, request_durable_build)
    app = app_by_id(app_id)
    if not app:
        raise HTTPException(status_code=404,
                            detail=f"Unknown supported app: {app_id}")
    factory = _get_factory(request)
    check_rate_limit(f"user:{user.user_id}", "mcp_build")
    try:
        source = install_source(app, base_url=getattr(body, "base_url", None))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    name = f"{app['name']} ({app['version']})"
    try:
        result = await request_durable_build(
            factory, user_id=user.user_id, method="spec", source=source,
            name=name, origin=ORIGIN_API,
            workflow_engine=getattr(request.app.state, "workflow_engine", None),
            required_operations=list(app["tested_operations"]),
        )
    except BuildAdmissionDenied as denied:
        status_code = 409 if denied.first_command_id else 429
        raise HTTPException(status_code=status_code, detail={
            "code": denied.reason.value,
            "message": denied.message,
            "retry_after_seconds": denied.retry_after_seconds,
            "accepted": False,
            "decision_id": denied.decision_id,
            **({"command_id": denied.first_command_id} if denied.first_command_id else {}),
        })
    await factory.audit_repo.log_event({
        "event_type": "SUPPORTED_APP_INSTALLED",
        "actor_id": user.user_id, "actor_type": "USER",
        "resource_id": result.get("build_id"),
        "details": {"app_id": app_id, "app_version": app["version"],
                    "tested_operations": app["tested_operations"]},
    })
    return {"message": f"Installing {name}. Poll /integrations/builds/{{build_id}} for progress.",
            "app_id": app_id, "app_version": app["version"],
            "documented_operations": app["documented_operations"],
            "tested_operations": app["tested_operations"], **result}


@router.post("/build-from-spec", status_code=status.HTTP_202_ACCEPTED)
@router.post("/build", status_code=status.HTTP_202_ACCEPTED)
async def build_from_spec(
    body: BuildFromSpecRequest, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Build an MCP integration from a raw OpenAPI/Swagger spec (JSON or YAML)."""
    factory = _get_factory(request)
    check_rate_limit(f"user:{user.user_id}", "mcp_build")

    try:
        spec_text = sanitize_text(body.spec, "spec", max_length=500_000)
    except InputValidationError as e:
        raise HTTPException(status_code=400, detail=e.message)

    return await _start_build(factory, user.user_id, "spec", spec_text, body.name, getattr(request.app.state, "workflow_engine", None))


@router.post("/build-python-tools", status_code=status.HTTP_202_ACCEPTED)
async def build_python_tools(body: BuildPythonToolsRequest, request: Request,
                             user: AuthenticatedUser = Depends(require_not_viewer)):
    """Generate and validate an isolated MCP server through durable admission."""
    import json
    from backend.services.isolated_runner import get_runner
    import os as _os
    # The ADMITTING process needs the runner TOPOLOGY declared; the
    # EXECUTING process (worker) configures the actual engine connection
    # (docs/managed-mcp-builds.md — the API never requires engine
    # authority to admit a durable build).
    configured = bool(getattr(get_runner(), "image", None)) or         bool(_os.environ.get("AGENTOS_RUNNER_IMAGE_DIGEST")) and         bool(_os.environ.get("AGENTOS_RUNNER_PROFILE_DIGEST")) and         bool(_os.environ.get("AGENTOS_RUNNER_DATABASE_URL") or
             _os.environ.get("AGENTOS_RUNNER_JOURNAL"))
    if not configured:
        raise HTTPException(503, "Managed MCP execution needs a configured isolated runner")
    source = json.dumps(body.model_dump(exclude={"name"}), allow_nan=False)
    if len(source.encode()) > 500_000:
        raise HTTPException(413, "Standalone build definition exceeds 500 KB")
    check_rate_limit(f"user:{user.user_id}", "mcp_build")
    return await _start_build(_get_factory(request), user.user_id, "python_tools", source, body.name,
        getattr(request.app.state, "workflow_engine", None),
        required_operations=[tool.get("name") for tool in body.tools])


@router.post("/build-from-url", status_code=status.HTTP_202_ACCEPTED)
async def build_from_url(
    body: BuildFromURLRequest, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Build from a URL pointing to an OpenAPI spec or API documentation."""
    factory = _get_factory(request)
    check_rate_limit(f"user:{user.user_id}", "mcp_build")

    try:
        url = sanitize_text(body.url, "url", max_length=2000)
    except InputValidationError as e:
        raise HTTPException(status_code=400, detail=e.message)

    return await _start_build(factory, user.user_id, body.method or "url", url, body.name, getattr(request.app.state, "workflow_engine", None))


@router.post("/build-from-website", status_code=status.HTTP_202_ACCEPTED)
async def build_from_website(
    body: BuildFromWebsiteRequest, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Build a Playwright website MCP for an app with no public API."""
    factory = _get_factory(request)
    check_rate_limit(f"user:{user.user_id}", "mcp_build")
    try:
        url = sanitize_text(body.url, "url", max_length=2000)
        notes = sanitize_text(body.notes or "", "notes", max_length=4000) if body.notes else ""
    except InputValidationError as e:
        raise HTTPException(status_code=400, detail=e.message)
    source = f"{url}\n{notes}".strip()
    return await _start_build(factory, user.user_id, "website", source, body.name, getattr(request.app.state, "workflow_engine", None))


@router.post("/build-from-prompt", status_code=status.HTTP_202_ACCEPTED)
async def build_from_prompt(
    body: BuildFromPromptRequest, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Build from a natural language description (lands in pending-review trust tier)."""
    factory = _get_factory(request)
    check_rate_limit(f"user:{user.user_id}", "mcp_build")

    try:
        prompt = sanitize_text(body.prompt, "prompt", max_length=5000)
    except InputValidationError as e:
        raise HTTPException(status_code=400, detail=e.message)

    return await _start_build(factory, user.user_id, "prompt", prompt, body.name, getattr(request.app.state, "workflow_engine", None))


@router.get("/builds/{build_id}")
async def get_build_status(
    build_id: str, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Poll the status, stage, and logs of an MCP factory build."""
    factory = _get_factory(request)
    build = await factory.mcp_repo.get_build(build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    if build.get("user_id") != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")
    return build


# ══════════════════════════════════════════════════════════════════
#  TEST / HEALTH / ENABLE / DISABLE / DELETE
# ══════════════════════════════════════════════════════════════════

@router.post("/{mcp_id}/test")
async def test_integration(
    mcp_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Test an integration: validate cached tools and run a safe live GET probe if possible."""
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or (mcp.get("owner") not in (user.user_id, "system") and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Integration not found")

    from datetime import datetime, timezone
    tools = await factory.mcp_repo.get_cached_tools(mcp_id=mcp_id)
    transport = str(mcp.get("transport") or "internal")

    try:
        if transport == "internal":
            if not tools:
                raise ValueError("No tools registered for this integration")
            # Evidence belongs only to the selected operation, never all cached tools.
            probe = {"status": "skipped", "reason": "no parameterless GET tool"}
            for tool in tools:
                op = tool.get("operation") or {}
                required = (tool.get("input_schema") or {}).get("required") or []
                if op.get("http_method") == "GET" and not required:
                    from backend.mcp.openapi_executor import execute_openapi_tool
                    # §6.4: preserve source provenance independently of test
                    # results — keep the prior _validation (provenance lives
                    # there) and let record_read_probe carry it forward.
                    tool["operation"] = op
                    await factory.mcp_repo.cache_tools(mcp_id, tools)
                    result = await execute_openapi_tool(
                        mcp, tool, {}, user_id=user.user_id, secrets_repo=factory.secrets_repo,
                    )
                    probe = {"status": "probed", "ok": result.get("ok"), "http_status": result.get("status"), "tool_name": tool.get("tool_name")}
                    if result.get("ok") is True:
                        from backend.services.tool_validation import record_read_probe
                        auth = mcp.get("auth") or {}
                        if isinstance(auth, str):
                            import json
                            auth = json.loads(auth)
                        tested = record_read_probe(tool, mcp.get("spec_hash"),
                                                   credential_binding_id_from(auth))
                        tools = [tested if t.get("tool_name") == tool.get("tool_name") else t for t in tools]
                        await factory.mcp_repo.cache_tools(mcp_id, tools)
                        await factory.mcp_repo.register_mcp({**mcp, "trust_tier": "verified"})
                    break
            if probe.get("status") == "skipped":
                return {"status": "untested", "tools_discovered": len(tools), "probe": probe}
            healthy = probe.get("ok") is True
            await factory.mcp_repo.update_mcp_health(
                mcp_id, "HEALTHY" if healthy else "UNHEALTHY", datetime.now(timezone.utc)
            )
            (circuit_breaker.record_success if healthy else circuit_breaker.record_failure)(mcp_id)
            return {
                "status": "healthy" if healthy else "unhealthy",
                "tools_discovered": len(tools),
                "validated_tool_count": _validated_operation_count(mcp, tools),
                "probe": probe,
            }

        # Remote MCP server (streamable HTTP / stdio)
        from backend.mcp.mcp_client import MCPClientManager
        discovered = await MCPClientManager.list_tools(mcp)
        await factory.mcp_repo.update_mcp_health(mcp_id, "HEALTHY", datetime.now(timezone.utc))
        circuit_breaker.record_success(mcp_id)
        return {"status": "healthy", "tools_discovered": len(discovered) if discovered else 0}
    except Exception as e:
        await factory.mcp_repo.set_mcp_enabled(mcp_id, False)
        await factory.mcp_repo.update_mcp_health(
            mcp_id, "UNHEALTHY", datetime.now(timezone.utc)
        )
        circuit_breaker.record_failure(mcp_id)
        return {"status": "unhealthy", "error": str(e)}


# ══════════════════════════════════════════════════════════════════
#  CREDENTIALS
# ══════════════════════════════════════════════════════════════════

class CredentialsRequest(BaseModel):
    api_key: str
    auth_type: Optional[str] = "API_KEY"   # API_KEY | OAUTH2 | BASIC


@router.post("/{mcp_id}/credentials")
async def set_credentials(
    mcp_id: str, body: CredentialsRequest, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Store an encrypted credential for this integration and link it via credential_ref."""
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or (mcp.get("owner") not in (user.user_id, "system") and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Integration not found")
    if mcp.get("owner") not in (user.user_id, "system") and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        api_key = sanitize_text(body.api_key, "api_key", max_length=4000)
    except InputValidationError as e:
        raise HTTPException(status_code=400, detail=e.message)

    from backend.security.secrets_vault import secrets_vault
    credential_ref = f"mcp:{mcp_id}:api_key"
    encrypted = secrets_vault.encrypt(api_key)
    # Invalidate before replacing a credential at the same reference.
    await factory.mcp_repo.set_mcp_enabled(mcp_id, False)
    tools = await factory.mcp_repo.get_cached_tools(mcp_id=mcp_id)
    for tool in tools:
        tool["operation"] = {k: v for k, v in (tool.get("operation") or {}).items() if k != "_validation"}
    await factory.mcp_repo.cache_tools(mcp_id, tools)
    await factory.secrets_repo.store_secret(user.user_id, credential_ref, encrypted)

    auth = mcp.get("auth") or {}
    if isinstance(auth, str):
        import json as _json
        auth = _json.loads(auth)
    new_type = (body.auth_type or "API_KEY").upper()
    # Preserve the scheme placement recorded at build time (header/query + header name).
    placement = {k: auth[k] for k in ("in", "name") if k in auth}
    auth.clear()
    auth["type"] = new_type
    if new_type not in ("BEARER", "BASIC", "OAUTH2", "OAUTH", "NONE"):
        auth.update(placement)
    auth["credential_ref"] = credential_ref
    await factory.mcp_repo.update_mcp_auth(mcp_id, auth)

    await factory.audit_repo.log_event({
        "event_type": "MCP_CREDENTIALS_SET",
        "actor_id": user.user_id, "actor_type": "USER",
        "resource_id": mcp_id,
        "details": {"auth_type": auth["type"]},
    })

    return {"mcp_id": mcp_id, "credential_ref": credential_ref, "auth_type": auth["type"]}


@router.get("/{mcp_id}/health")
async def get_health(
    mcp_id: str, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Get integration health and circuit breaker status."""
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or (mcp.get("owner") not in (user.user_id, "system") and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Integration not found")

    return {
        "mcp_id": mcp_id,
        "health": mcp.get("health", "UNKNOWN"),
        "health_updated_at": mcp.get("health_updated_at"),
        "circuit_breaker": circuit_breaker.get_status(mcp_id),
    }


# ══════════════════════════════════════════════════════════════════
#  SHARED CATALOG (audit §9: "publish user-built MCPs — one ACL away")
# ══════════════════════════════════════════════════════════════════

@router.post("/{mcp_id}/publish")
async def publish_integration(
    mcp_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Publish an integration to the shared catalog.

    Gate: only the owner may publish, and only *verified* integrations
    (probe-passed OpenAPI builds) — pending-review or website browser
    tools stay private, so nothing unreviewed is ever shared."""
    check_rate_limit(f"user:{user.user_id}", "general")
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or mcp.get("owner") != user.user_id:
        raise HTTPException(status_code=404, detail="Integration not found")
    if str(mcp.get("trust_tier")) != "verified":
        raise HTTPException(
            status_code=400,
            detail="Only verified integrations (live-probe passed) can be published. "
                   "Build from an OpenAPI URL or spec to earn the verified tier.",
        )
    await factory.mcp_repo.set_public(mcp_id, True)
    await factory.audit_repo.log_event({
        "event_type": "MCP_PUBLISHED",
        "actor_id": user.user_id, "actor_type": "USER",
        "resource_id": mcp_id,
        "details": {"name": mcp.get("name"), "trust_tier": mcp.get("trust_tier")},
    })
    return {"mcp_id": mcp_id, "is_public": True}


@router.post("/{mcp_id}/unpublish")
async def unpublish_integration(
    mcp_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Remove an integration from the shared catalog (owner or admin)."""
    check_rate_limit(f"user:{user.user_id}", "general")
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or (mcp.get("owner") != user.user_id and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Integration not found")
    await factory.mcp_repo.set_public(mcp_id, False)
    return {"mcp_id": mcp_id, "is_public": False}


def _catalog_entry(mcp: dict, tools: list) -> dict:
    """Catalog view: metadata + tool names only. Never spec_json, never auth
    material, never the owner's identity."""
    return {
        "mcp_id": mcp.get("mcp_id"),
        "name": mcp.get("name"),
        "description": mcp.get("description"),
        "source_type": mcp.get("source_type"),
        "trust_tier": mcp.get("trust_tier"),
        "spec_provenance": mcp.get("spec_provenance", "unknown"),
        "test_coverage": mcp.get("test_coverage") or {},
        "spec_version": mcp.get("spec_version"),
        "updated_at": mcp.get("updated_at"),
        "tools": [{"name": t.get("tool_name"), "description": t.get("description")} for t in tools],
        "tool_count": len(tools),
    }


@router.post("/{mcp_id}/install", status_code=status.HTTP_201_CREATED)
async def install_from_catalog(
    mcp_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Install a published integration into the caller's own workspace.

    Clones the manifest and tool schemas with a fresh per-user id. The
    owner's credentials are NEVER copied — the auth *placement* metadata
    (header vs query, key name) carries over; each user attaches their
    own secret via /integrations/{id}/credentials or the Vault."""
    check_rate_limit(f"user:{user.user_id}", "general")
    import hashlib
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or not mcp.get("is_public"):
        raise HTTPException(status_code=404, detail="Integration not found in the catalog")

    new_id = hashlib.sha256(f"{user.user_id}:{mcp.get('spec_hash') or mcp_id}".encode()).hexdigest()[:16]
    existing = await factory.mcp_repo.get_mcp(new_id)
    if existing:
        return {"mcp_id": new_id, "installed": False, "message": "Already installed"}

    auth = mcp.get("auth") or {}
    if isinstance(auth, str):
        try:
            import json as _json
            auth = _json.loads(auth)
        except Exception:
            auth = {}
    auth = {k: v for k, v in dict(auth).items() if k != "credential_ref"}

    clone = dict(mcp)
    clone["mcp_id"] = new_id
    clone["owner"] = user.user_id
    clone["auth"] = auth
    clone["is_public"] = False
    clone["is_enabled"] = True
    # Validate the manifest shape, defaulting anything a legacy row lacks.
    from backend.models.mcp_schemas import MCPManifest
    manifest_fields = set(MCPManifest.model_fields)
    payload = {k: v for k, v in clone.items() if k in manifest_fields}
    payload.setdefault("transport", "internal")
    payload.setdefault("endpoint", f"internal://openapi/{new_id}")
    payload.setdefault("version", str(clone.get("spec_version") or "1.0.0"))
    MCPManifest(**payload)  # validate shape before persisting
    await factory.mcp_repo.register_mcp(payload)

    tools = await factory.mcp_repo.get_cached_tools(mcp_id)
    rekeyed = []
    for t in tools:
        t = dict(t)
        t["mcp_id"] = new_id
        rekeyed.append(t)
    if rekeyed:
        await factory.mcp_repo.cache_tools(new_id, rekeyed)

    await factory.audit_repo.log_event({
        "event_type": "MCP_INSTALLED",
        "actor_id": user.user_id, "actor_type": "USER",
        "resource_id": new_id,
        "details": {"from_catalog": mcp_id, "name": mcp.get("name"), "tools": len(rekeyed)},
    })
    return {"mcp_id": new_id, "installed": True, "tools": len(rekeyed), "name": mcp.get("name")}


@router.post("/{mcp_id}/enable")
async def enable_integration(
    mcp_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Enable an integration."""
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or (mcp.get("owner") not in (user.user_id, "system") and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Integration not found")
    if mcp.get("trust_tier") != "verified":
        raise HTTPException(status_code=409, detail="Validate this connector revision before enabling it")
    from backend.services.tool_validation import has_valid_read_probe, credential_binding_id_from, validation_provenance
    auth = mcp.get("auth") or {}
    if isinstance(auth, str):
        import json
        auth = json.loads(auth)
    if mcp.get("transport") == "internal":
        tools = await factory.mcp_repo.get_cached_tools(mcp_id=mcp_id)
        binding = credential_binding_id_from(auth)
    if not any(has_valid_read_probe(t, mcp.get("spec_hash"), binding) for t in tools):
            raise HTTPException(status_code=409, detail="No operation has current validation evidence; connect credentials and test first")
    await factory.mcp_repo.set_mcp_enabled(mcp_id, True)
    return {"mcp_id": mcp_id, "is_enabled": True}


@router.post("/{mcp_id}/disable")
async def disable_integration(
    mcp_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Disable an integration."""
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or (mcp.get("owner") not in (user.user_id, "system") and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Integration not found")
    await factory.mcp_repo.set_mcp_enabled(mcp_id, False)
    return {"mcp_id": mcp_id, "is_enabled": False}


@router.delete("/{mcp_id}", status_code=status.HTTP_200_OK)
async def delete_integration(
    mcp_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Delete an integration and its cached tools."""
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or (mcp.get("owner") not in (user.user_id, "system") and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Integration not found")
    deleted = await factory.mcp_repo.delete_mcp(mcp_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Integration not found")

    await factory.audit_repo.log_event({
        "event_type": "MCP_DELETED",
        "actor_id": user.user_id, "actor_type": "USER",
        "resource_id": mcp_id,
        "details": {},
    })

    return {"mcp_id": mcp_id, "deleted": True}


@router.get('/builds/{build_id}/events')
async def stream_build_events(build_id: str, request: Request,
                              user: AuthenticatedUser = Depends(get_current_user)):
    """Stream persisted build state; reconnects recover the latest status."""
    import asyncio
    import json
    from fastapi.responses import StreamingResponse
    factory = _get_factory(request)
    build = await factory.mcp_repo.get_build(build_id)
    if not build or (build.get('user_id') != user.user_id and not user.is_admin()):
        raise HTTPException(status_code=404, detail='Build not found')

    async def events():
        previous = None
        while not await request.is_disconnected():
            current = await factory.mcp_repo.get_build(build_id)
            if not current:
                return
            payload = safe_json_dumps(current)
            if payload != previous:
                yield f'event: build\ndata: {payload}\n\n'
                previous = payload
            else:
                yield ': heartbeat\n\n'
            if current.get('status') in ('success', 'error'):
                return
            await asyncio.sleep(1)
    return StreamingResponse(events(), media_type='text/event-stream',
                             headers={'Cache-Control':'no-cache', 'X-Accel-Buffering':'no'})


@router.get('/{mcp_id}/export')
async def export_integration(mcp_id: str, request: Request,
                             user: AuthenticatedUser = Depends(get_current_user)):
    from fastapi.responses import Response
    from backend.mcp.builder.portable_server import export_server
    factory = _get_factory(request)
    manifest = await factory.mcp_repo.get_mcp(mcp_id)
    if not manifest or (manifest.get('owner') not in (user.user_id, 'system') and not user.is_admin()):
        raise HTTPException(status_code=404, detail='Integration not found')
    tools = await factory.mcp_repo.get_cached_tools(mcp_id)
    try:
        archive = export_server(manifest, tools)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(archive, media_type='application/zip', headers={'Content-Disposition':'attachment; filename="generated-mcp.zip"'})

# Immutable definition history. Credentials are not included in revision responses.
@router.get('/{mcp_id}/revisions')
async def list_connector_revisions(mcp_id: str, request: Request,
                                   user: AuthenticatedUser = Depends(get_current_user)):
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or (mcp.get('owner') not in (user.user_id, 'system') and not user.is_admin()):
        raise HTTPException(status_code=404, detail='Integration not found')
    revisions = await factory.mcp_repo.list_revisions(mcp_id)
    return {'revisions': [{'revision_id': r['revision_id'], 'created_at': r['created_at'],
                           'tool_count': len(r['tools']), 'version': r['manifest'].get('version')}
                          for r in revisions]}


@router.post('/{mcp_id}/revisions', status_code=201)
async def checkpoint_connector(mcp_id: str, request: Request,
                               user: AuthenticatedUser = Depends(require_not_viewer)):
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or (mcp.get('owner') != user.user_id and not user.is_admin()):
        raise HTTPException(status_code=404, detail='Integration not found')
    from backend.services.connector_revisions import checkpoint
    try:
        revision = await checkpoint(factory.mcp_repo, mcp_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await factory.audit_repo.log_event({'event_type': 'CONNECTOR_CHECKPOINT', 'actor_id': user.user_id,
                                       'actor_type': 'USER', 'resource_id': mcp_id,
                                       'details': {'revision_id': revision['revision_id']}})
    return {'revision_id': revision['revision_id']}


@router.post('/{mcp_id}/revisions/{revision_id}/restore', status_code=201)
async def restore_connector_revision(mcp_id: str, revision_id: str, request: Request,
                                     user: AuthenticatedUser = Depends(require_not_viewer)):
    factory = _get_factory(request)
    mcp = await factory.mcp_repo.get_mcp(mcp_id)
    if not mcp or (mcp.get('owner') not in (user.user_id, 'system') and not user.is_admin()):
        raise HTTPException(status_code=404, detail='Integration not found')
    check_rate_limit(f'user:{user.user_id}', 'mcp_build')
    await check_shared_rate_limit(f'user:{user.user_id}', group='mcp_build')
    from backend.services.connector_revisions import restore_copy
    try:
        restored_id = await restore_copy(factory.mcp_repo, mcp_id, revision_id, user.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await factory.audit_repo.log_event({'event_type': 'CONNECTOR_RESTORED', 'actor_id': user.user_id,
                                       'actor_type': 'USER', 'resource_id': restored_id,
                                       'details': {'source_mcp_id': mcp_id, 'revision_id': revision_id}})
    return {'mcp_id': restored_id, 'is_enabled': False,
            'message': 'Restored as a disabled copy. Reconnect credentials and test before enabling.'}
