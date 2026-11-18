"""Invoke a built capability using its durable parent link and current eligibility.

§29.14 revision: discovery searches the owner's eligible INSTALLED
operations FIRST. When a run's task carries no build reference (a fresh
run of a saved automation whose capability already exists), the
installed connector that publishes the operation is resolved directly —
through the same eligibility checks (owner, enabled, current
validation evidence) as the build-child path. A run that DOES carry a
wait keeps its pinned build identity.
"""
import json

from backend.repositories.wait_repository import get_wait_repo
from backend.services.connector_eligibility import evaluate_capability_readiness


async def _resolve_installed_operation(router, user_id: str, operation: str):
    """The owner's enabled connector publishing `operation` with CURRENT
    validation evidence — or None when no installed capability covers it."""
    if not operation or "__" in operation:
        raise ValueError("A single generated operation name is required")
    mcps = await router.mcp_repo.list_mcps(user_id)
    for mcp in mcps or []:
        if not mcp.get("is_enabled") or mcp.get("owner") != user_id:
            continue
        cached = await router.mcp_repo.get_cached_tools(mcp["mcp_id"])
        for tool in cached or []:
            tool_name = tool.get("tool_name") if isinstance(tool, dict) \
                else getattr(tool, "tool_name", None)
            if tool_name != operation:
                continue
            runs = await router.mcp_repo.list_validation_runs(mcp["mcp_id"])
            readiness = evaluate_capability_readiness(
                mcp, {"status": "success", "mcp_id": mcp["mcp_id"],
                      "user_id": user_id,
                      "required_operations": [operation]}, runs)
            if readiness["ready"]:
                return mcp
    return None


async def invoke_built_capability(router, workflow_repo, context, inputs):
    if router is None:
        raise RuntimeError("Tool router unavailable")
    build_id = inputs.get("build_id")
    if not build_id:
        waits = get_wait_repo()
        wait = await waits.latest_wait(context["run_id"], context["task_id"]) if waits else None
        if not wait or not str(wait.get("state", "")).startswith("resolved"):
            # §29.14 revision: no build reference — discover the INSTALLED
            # capability first. A run of a saved automation whose
            # capability already exists goes straight to the pinned
            # installed revision through the same eligibility checks.
            operation = inputs.get("operation")
            if isinstance(operation, str) and operation:
                installed = await _resolve_installed_operation(
                    router, context.get("user_id"), operation)
                if installed is not None:
                    arguments = inputs.get("arguments", {})
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be an object")
                    return await router.execute_tool_safe(
                        f"{installed['mcp_id']}__{operation}", arguments,
                        context=context)
            raise RuntimeError("Built capability has not resolved its parent wait")
        child = await workflow_repo.get_run(wait["child_run_id"])
        if not child or child.get("user_id") != context["user_id"] or child.get("status") != "COMPLETED":
            raise RuntimeError("Child build is unavailable or unauthorized")
        builds = [t for t in child.get("tasks", []) if t.get("agent") == "core.mcp_build"]
        if len(builds) != 1:
            raise RuntimeError("Child build reference is ambiguous")
        child_inputs = builds[0].get("input_data") or {}
        if isinstance(child_inputs, str):
            child_inputs = json.loads(child_inputs)
        build_id = child_inputs.get("build_id")
    build = await router.mcp_repo.get_build(build_id)
    if not build or build.get("user_id") != context["user_id"]:
        raise RuntimeError("Build is not accessible")
    operation = inputs.get("operation")
    if not isinstance(operation, str) or not operation or "__" in operation:
        raise ValueError("A single generated operation name is required")
    connector = await router.mcp_repo.get_mcp(build.get("mcp_id")) if build.get("mcp_id") else None
    runs = await router.mcp_repo.list_validation_runs(build["mcp_id"]) if connector else []
    readiness = evaluate_capability_readiness(connector, {**build, "required_operations": [operation]}, runs)
    if not readiness["ready"]:
        raise RuntimeError("Built capability is not ready: " + str(readiness["next_action"]))
    arguments = inputs.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object")
    return await router.execute_tool_safe(f"{build['mcp_id']}__{operation}", arguments, context=context)
