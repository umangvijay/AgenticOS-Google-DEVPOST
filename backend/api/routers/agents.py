"""User-owned specialist agent plugins and lifecycle."""
import hashlib
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import AuthenticatedUser, require_not_viewer
from backend.models.plugin import PluginManifest, PluginLifecycleState, PluginScope
from backend.security.rate_limiter import check_rate_limit

router = APIRouter(prefix="/agents", tags=["agents"])

class InstallAgentRequest(BaseModel):
    manifest: dict[str, Any]

class AgentToolsRequest(BaseModel):
    required_tools: list[str] = Field(min_length=1, max_length=64)


def _registry(request: Request):
    registry = getattr(request.app.state, "plugin_registry", None)
    if not registry:
        raise HTTPException(status_code=503, detail="Agent registry is not available")
    return registry


@router.get("")
async def list_agents(request: Request, user: AuthenticatedUser = Depends(require_not_viewer)):
    records = await _registry(request).repository.list_all(user.user_id)
    return {"agents": [
        {**agent.model_dump(), "plugin_record_id": record.id,
         "plugin_name": record.manifest.name, "state": record.state.value}
        for record in records if record.state == PluginLifecycleState.ENABLED
        for agent in record.manifest.agents
    ]}


@router.get("/plugins")
async def list_agent_plugins(request: Request, user: AuthenticatedUser = Depends(require_not_viewer)):
    records = await _registry(request).repository.list_all(user.user_id)
    return {"plugins": [record.model_dump(mode="json") for record in records
                         if record.installed_by == user.user_id and record.manifest.scope == PluginScope.USER]}


@router.get("/tools")
async def available_agent_tools(request: Request, user: AuthenticatedUser = Depends(require_not_viewer)):
    factory = getattr(request.app.state, "factory", None)
    if not factory:
        raise HTTPException(status_code=503, detail="Repositories are not available")
    tools = await request.app.state.tool_router.get_tool_catalog(user.user_id)
    return {"tools": tools}


@router.post("/plugins", status_code=201)
async def install_agent_plugin(body: InstallAgentRequest, request: Request,
                               user: AuthenticatedUser = Depends(require_not_viewer)):
    check_rate_limit(f"user:{user.user_id}", "general")
    data = dict(body.manifest)
    if data.get("scope", "USER") != "USER":
        raise HTTPException(status_code=403, detail="Only user-scoped agent plugins may be installed here")
    data["scope"] = PluginScope.USER.value
    # The server owns integrity metadata; never trust a client-computed signature.
    data["manifest_hash"] = "pending"
    manifest = PluginManifest.model_validate(data)
    unsigned = manifest.model_dump_json(exclude={"manifest_hash"}, exclude_none=True)
    manifest.manifest_hash = hashlib.sha256(unsigned.encode()).hexdigest()
    existing = await _registry(request).repository.list_all(user.user_id)
    requested_ids = {agent.agent_id for agent in manifest.agents}
    collisions = sorted({agent.agent_id for record in existing for agent in record.manifest.agents} & requested_ids)
    if collisions:
        raise HTTPException(status_code=409, detail={"message":"Agent IDs must be unique across your installed plugins", "agent_ids":collisions})
    catalog = await request.app.state.tool_router.get_tool_catalog(user.user_id)
    available = {item["agent_tool_name"] for item in catalog}
    missing = set(manifest.required_tools) - available
    if missing:
        raise HTTPException(status_code=400, detail={"message":"Plugin requests tools that are not currently available to this user", "missing_tools":sorted(missing)})
    try:
        record = await _registry(request).install_plugin(manifest.model_dump_json(), user.user_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Agent plugin validation failed: {exc}") from exc
    return {"plugin": record.model_dump(mode="json")}


@router.post("/plugins/{plugin_id}/enable")
async def enable_agent_plugin(plugin_id: str, body: AgentToolsRequest, request: Request,
                              user: AuthenticatedUser = Depends(require_not_viewer)):
    service = _registry(request)
    record = await service.repository.get_by_id(plugin_id)
    if not record or (record.installed_by != user.user_id and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Agent plugin not found")
    catalog = await request.app.state.tool_router.get_tool_catalog(user.user_id)
    available = {item["agent_tool_name"] for item in catalog}
    if not set(body.required_tools).issubset(available):
        raise HTTPException(status_code=400, detail="Every selected tool must be available to this user")
    declared = set(record.manifest.required_tools)
    if not set(body.required_tools).issubset(declared):
        raise HTTPException(status_code=400, detail="Selected tools must be declared in the plugin manifest")
    record.manifest.required_tools = body.required_tools
    for agent in record.manifest.agents:
        agent.allowed_tools = [tool for tool in agent.allowed_tools if tool in body.required_tools]
        if not agent.allowed_tools:
            raise HTTPException(status_code=400, detail=f"Select at least one tool for agent {agent.display_name}")
    # Keep the verified manifest signature aligned with the permission edit.
    unsigned = record.manifest.model_dump_json(exclude={"manifest_hash"}, exclude_none=True)
    record.manifest.manifest_hash = hashlib.sha256(unsigned.encode()).hexdigest()
    record.state = PluginLifecycleState.INSTALLED
    await service.repository.save(record)
    try:
        await service.enable_plugin(plugin_id, user.user_id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status":"enabled", "plugin_id":plugin_id, "agents":[a.agent_id for a in record.manifest.agents]}


@router.post("/plugins/{plugin_id}/disable")
async def disable_agent_plugin(plugin_id: str, request: Request,
                               user: AuthenticatedUser = Depends(require_not_viewer)):
    service = _registry(request)
    record = await service.repository.get_by_id(plugin_id)
    if not record or (record.installed_by != user.user_id and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Agent plugin not found")
    await service.disable_plugin(plugin_id, user.user_id)
    return {"status":"disabled", "plugin_id":plugin_id}
