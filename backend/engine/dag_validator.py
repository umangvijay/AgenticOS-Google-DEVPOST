import logging
import re

from backend.models.schemas import WorkflowDefinition

logger = logging.getLogger(__name__)

_HTTP_METHOD_BY_ACTION = {
    "fetch": "GET",
    "get": "GET",
    "read": "GET",
    "post": "POST",
    "create": "POST",
    "put": "PUT",
    "update": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
}


def _extract_url_from_intent(intent: dict, extra_text: str = "") -> str | None:
    """Best-effort URL extraction when the planner omits or truncates URL fields."""
    blobs = [extra_text]
    for field in ("target", "url", "context", "goal"):
        val = intent.get(field, "")
        if isinstance(val, str):
            blobs.append(val)
    text = " ".join(b for b in blobs if b)

    match = re.search(r"https?://[^\s\"'<>]+", text)
    if match:
        url = match.group(0).rstrip(").,;]")
        if _hostname_present(url):
            return url

    match = re.search(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s\"'<>]*)?)", text, re.I)
    if match:
        host_path = match.group(1).rstrip(").,;]")
        if "@" not in host_path:
            return "https://" + host_path
    return None


def _hostname_present(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        return bool(urlparse(url).hostname)
    except Exception:
        return False


def _find_catalog_tool(intent: dict, catalog: list) -> dict | None:
    """Match intent action/target to a registered integration tool."""
    if not catalog:
        return None
    action = str(intent.get("action", "")).strip()
    target = str(intent.get("target", "")).lower()
    context = str(intent.get("context", "")).lower()
    search = f"{action} {target} {context}".lower()

    # Exact tool name match (e.g. action=getInventory)
    if action:
        for tool in catalog:
            if tool.get("name", "").lower() == action.lower():
                return tool

    # Tool name appears in intent text
    for tool in catalog:
        name = tool.get("name", "").lower()
        if name and name in search:
            return tool

    # Integration name match (e.g. "Petstore" in target) — pick first tool from that MCP
    for tool in catalog:
        mcp_name = tool.get("mcp_name", "").lower()
        if mcp_name and mcp_name in search:
            return tool

    return None


def enrich_plan_from_intent(
    definition: WorkflowDefinition,
    intent: dict,
    catalog: list | None = None,
    goal: str = "",
) -> WorkflowDefinition:
    """Fill missing HTTP/health URLs or reroute to OrchestratorAgent when a catalog tool matches."""
    url = _extract_url_from_intent(intent, extra_text=goal)
    action = str(intent.get("action", "fetch")).lower()
    default_method = _HTTP_METHOD_BY_ACTION.get(action, "GET")
    matched_tool = _find_catalog_tool(intent, catalog or [])

    for task in definition.tasks:
        if task.agent == "core.http" and not _hostname_present(str(task.input_data.get("url") or "")):
            if matched_tool:
                task.agent = "OrchestratorAgent"
                task.tool = matched_tool.get("agent_tool_name")
                task.input_data = {
                    "goal": intent.get("context") or intent.get("target") or action,
                    "tool": matched_tool.get("agent_tool_name"),
                }
                logger.info(
                    "Rerouted core.http task %s to OrchestratorAgent tool %s",
                    task.task_id,
                    task.tool,
                )
                continue
            if url:
                task.input_data["url"] = url
        if task.agent == "core.http" and not task.input_data.get("method"):
            task.input_data["method"] = default_method
        if task.agent == "core.health" and url and not _hostname_present(str(task.input_data.get("url") or "")):
            task.input_data["url"] = url

    return definition


def ensure_unique_task_ids(definition: WorkflowDefinition) -> WorkflowDefinition:
    """Rename colliding planner task_ids in place so validate_dag can succeed."""
    used = set()
    for task in definition.tasks:
        raw = (task.task_id or "task").strip() or "task"
        candidate = raw
        n = 2
        while candidate in used:
            candidate = f"{raw}_{n}"
            n += 1
        if candidate != task.task_id:
            logger.warning("Renamed duplicate task_id %s -> %s", task.task_id, candidate)
            task.task_id = candidate
        used.add(task.task_id)
    return definition


def _is_orchestrator_agent(agent: str) -> bool:
    name = (agent or "").strip().lower()
    return name in ("orchestratoragent", "orchestrator") or "orchestrator" in name


def wire_mcp_then_use(definition: WorkflowDefinition) -> WorkflowDefinition:
    """Order MCP consumers after builds without discarding invalid dependencies."""
    mcp_ids = [t.task_id for t in definition.tasks if t.agent == "core.mcp_build"]
    for task in definition.tasks:
        if mcp_ids and _is_orchestrator_agent(task.agent):
            missing = [mid for mid in mcp_ids if mid not in task.dependencies]
            if missing:
                task.dependencies = list(task.dependencies) + missing
                logger.info("Wired %s to wait on MCP build(s) %s", task.task_id, missing)
    return definition


def prepare_dag(definition: WorkflowDefinition) -> WorkflowDefinition:
    wire_mcp_then_use(definition)
    validate_dag(definition)
    validate_inputs_and_references(definition)
    return definition

class DAGValidationError(Exception):
    pass

def validate_dag(definition: WorkflowDefinition) -> None:
    task_ids = set()
    dependencies_map = {}
    
    # 1. Duplicate task IDs and basic validation (prepare_dag uniquifies first)
    for task in definition.tasks:
        if task.task_id in task_ids:
            raise DAGValidationError(f"Duplicate task ID found: {task.task_id}")
        
        if task.timeout_seconds <= 0:
            raise DAGValidationError(f"Invalid timeout for task {task.task_id}")
            
        if task.max_retries < 0:
            raise DAGValidationError(f"Invalid max_retries for task {task.task_id}")
            
        # Agent names are dynamically generated by the PlannerAgent.
        # Validation only checks structural correctness, not agent existence.
        if not task.agent or not task.agent.strip():
            raise DAGValidationError(f"Empty agent name for task {task.task_id}")
            
        task_ids.add(task.task_id)
        dependencies_map[task.task_id] = task.dependencies

    # 2. Check for unknown or self dependencies
    for task_id, deps in dependencies_map.items():
        for dep in deps:
            if dep not in task_ids:
                raise DAGValidationError(f"Unknown dependency '{dep}' for task '{task_id}'")
            if dep == task_id:
                raise DAGValidationError(f"Self-dependency detected for task '{task_id}'")
                
    # 3. Detect cycles using DFS
    visited = set()
    stack = set()
    
    def dfs(current_id: str):
        visited.add(current_id)
        stack.add(current_id)
        
        for dep in dependencies_map.get(current_id, []):
            if dep not in visited:
                dfs(dep)
            elif dep in stack:
                raise DAGValidationError(f"Cycle detected in DAG involving task: {dep}")
                
        stack.remove(current_id)

    for task_id in task_ids:
        if task_id not in visited:
            dfs(task_id)

    logger.info(f"DAG validation successful for {len(task_ids)} tasks.")


def validate_inputs_and_references(definition: WorkflowDefinition) -> None:
    """Reject missing core inputs and references that cannot be scheduled safely."""
    from backend.config.settings import settings
    tasks = {task.task_id: task for task in definition.tasks}
    if len(tasks) > settings.MAX_WORKFLOW_TASKS:
        raise DAGValidationError("Workflow exceeds the configured task limit")
    if not tasks:
        raise DAGValidationError("A workflow must contain at least one task")
    required = {
        "core.http": ("url",), "core.health": ("url",),
        "core.email": ("to", "subject", "body"),
        "core.mcp_build": ("source", "method"),
        "core.set": ("fields",), "core.if": ("condition",),
    }
    supported = {*required, "core.chat", "core.merge", "core.loop"}

    def ancestors(task):
        result = set(task.dependencies)
        for dep in task.dependencies:
            result.update(ancestors(tasks[dep]))
        return result

    def references(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from references(child)
        elif isinstance(value, list):
            for child in value:
                yield from references(child)
        elif isinstance(value, str):
            yield from re.findall(r"\{\{\s*(.*?)\s*\}\}", value)

    for task in definition.tasks:
        if task.timeout_seconds > settings.MAX_TASK_TIMEOUT_SECONDS or task.max_retries > settings.MAX_TASK_RETRIES:
            raise DAGValidationError(f"Task {task.task_id} exceeds configured execution limits")
        if task.agent.startswith("core.") and task.agent not in supported:
            raise DAGValidationError(f"Unknown core node: {task.agent}")
        for field in required.get(task.agent, ()):
            value = task.input_data.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise DAGValidationError(f"Task {task.task_id} requires {field}")
        allowed_sources = ancestors(task)
        for expr in references(task.input_data):
            parts = expr.strip().split('.')
            if len(parts) < 3 or parts[0] != "tasks" or parts[2] != "output":
                raise DAGValidationError(f"Unsupported workflow reference: {expr}")
            if parts[1] not in allowed_sources:
                raise DAGValidationError(f"Task {task.task_id} references non-dependency {parts[1]}")
