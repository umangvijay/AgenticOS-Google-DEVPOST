import logging
import hashlib
import uuid
from typing import Dict, Any, List, Optional

from backend.repositories.mcp_repository import MCPRepository
from backend.mcp.tool_policy import ToolPolicy
from backend.mcp.mcp_client import MCPClientManager
from backend.mcp.openapi_executor import execute_openapi_tool
from backend.services.approvals_engine import ApprovalsEngine, ApprovalRequiredException
from backend.models.security import AutonomyLevel, RiskLevel
from backend.mcp.circuit_breaker import circuit_breaker
from backend.engine.idempotency_guard import IdempotencyGuard
from backend.models.mcp_schemas import CachedToolDefinition, MCPManifest, MCPHealthStatus, AuthMetadata, AuthType, MCPTransportType, ConnectorState

logger = logging.getLogger(__name__)


class ToolRouterError(Exception):
    pass


def _as_manifest(mcp: Any) -> MCPManifest:
    if isinstance(mcp, MCPManifest):
        return mcp
    data = dict(mcp)
    auth = data.get("auth") or {}
    if isinstance(auth, str):
        import json
        try:
            auth = json.loads(auth)
        except Exception:
            auth = {"type": auth}
    data["auth"] = AuthMetadata(**auth) if isinstance(auth, dict) else AuthMetadata()
    # Generated declarative adapters run inside the shared runtime — that
    # is INTERNAL transport by definition. Storage rows may omit the key
    # (registry payloads never carried it); defaulting here is the
    # truthful value, not a guess.
    if not data.get("transport"):
        data["transport"] = MCPTransportType.INTERNAL
    if isinstance(data.get("transport"), str):
        try:
            data["transport"] = MCPTransportType(data["transport"])
        except ValueError:
            data["transport"] = MCPTransportType.INTERNAL
    # Legacy rows may carry a non-contract state (e.g. "registered") or a
    # NULL endpoint; map to the nearest truthful contract value instead of
    # dropping the tool from the catalog.
    if data.get("state") not in ("", None) and \
            not isinstance(data.get("state"), ConnectorState):
        try:
            data["state"] = ConnectorState(str(data["state"]))
        except ValueError:
            data["state"] = ConnectorState.PENDING_CREDENTIALS
    if data.get("endpoint") is None:
        data["endpoint"] = ""
    # Storage-shape aliases: pg rows key identity as `id`; the manifest
    # contract is mcp_id. Same for coverage JSON stored as a string.
    if not data.get("mcp_id"):
        data["mcp_id"] = data.get("id") or ""
    if isinstance(data.get("test_coverage"), str):
        import json as _json
        try:
            data["test_coverage"] = _json.loads(data["test_coverage"]) or {}
        except Exception:
            data["test_coverage"] = {}
    return MCPManifest(**{k: v for k, v in data.items() if k in MCPManifest.model_fields})


def _as_tool(t: Any) -> CachedToolDefinition:
    if isinstance(t, CachedToolDefinition):
        return t
    data = dict(t)
    data.setdefault("tool_name", data.get("name") or "unknown")
    data.setdefault("mcp_version", "1.0.0")
    data.setdefault("input_schema", data.get("inputSchema") or {})
    from datetime import datetime, timezone
    data.setdefault("discovered_at", datetime.now(timezone.utc))
    data.setdefault("expires_at", datetime.now(timezone.utc))
    if "risk_level" in data and not isinstance(data["risk_level"], RiskLevel):
        try:
            data["risk_level"] = RiskLevel(int(data["risk_level"]))
        except Exception:
            data["risk_level"] = RiskLevel.CRITICAL
    return CachedToolDefinition(**{k: v for k, v in data.items() if k in CachedToolDefinition.model_fields})


class ToolRouter:
    def __init__(self, mcp_repo: MCPRepository, policy: ToolPolicy, approvals_engine: Optional[ApprovalsEngine] = None, idempotency_repo=None, secrets_repo=None):
        self.mcp_repo = mcp_repo
        self.policy = policy
        self.approvals_engine = approvals_engine
        self.idempotency_guard = IdempotencyGuard(idempotency_repo) if idempotency_repo else None
        self.secrets_repo = secrets_repo

    async def get_tool_catalog(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        tools = await self.mcp_repo.get_cached_tools()
        catalog = []
        for raw in tools:
            try:
                tool = _as_tool(raw)
                mcp_raw = await self.mcp_repo.get_mcp(tool.mcp_id)
                if not mcp_raw:
                    continue
                mcp = _as_manifest(mcp_raw)
                if user_id and mcp.owner not in (user_id, "system"):
                    continue
                if not mcp.is_enabled:
                    continue
                policy_result = self.policy.is_allowed(tool, mcp)
                if policy_result.allowed:
                    catalog.append({
                        "name": tool.tool_name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                        "agent_tool_name": f"{tool.mcp_id}__{tool.tool_name}",
                        "mcp_id": tool.mcp_id,
                        "mcp_name": mcp.name,
                    })
            except Exception:
                logger.exception("Skipping invalid catalog tool")
                continue
        return catalog

    async def execute_tool(self, agent_tool_name: str, arguments: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Any:
        if "__" not in agent_tool_name:
            raise ToolRouterError(f"Invalid tool name format: {agent_tool_name}")

        mcp_id, tool_name = agent_tool_name.split("__", 1)
        mcp_raw = await self.mcp_repo.get_mcp(mcp_id)
        if not mcp_raw:
            raise ToolRouterError(f"Unknown MCP Server: {mcp_id}")
        mcp = _as_manifest(mcp_raw)

        user_id = (context or {}).get("user_id")
        if not user_id or mcp.owner not in (user_id, "system"):
            raise ToolRouterError("Integration is not accessible to this user")

        cached_tools = await self.mcp_repo.get_cached_tools(mcp_id)
        raw_tool = next((t for t in cached_tools if (t.get("tool_name") if isinstance(t, dict) else t.tool_name) == tool_name), None)
        if not raw_tool:
            raise ToolRouterError(f"Unknown tool '{tool_name}' on MCP '{mcp_id}'")
        tool = _as_tool(raw_tool)

        policy_result = self.policy.is_allowed(tool, mcp)
        if not policy_result.allowed:
            raise ToolRouterError(f"Policy Denied: {policy_result.reason}")

        context = context or {}
        if self.approvals_engine:
            autonomy_level = context.get("autonomy_level", AutonomyLevel.L0_MANUAL)
            if self.approvals_engine.requires_approval(autonomy_level, tool.risk_level):
                approved_request = context.get("approved_request")
                if approved_request:
                    from backend.models.security import ApprovalRequest
                    if isinstance(approved_request, dict):
                        approved_request = ApprovalRequest(**approved_request)
                    bound_arguments = dict(arguments)
                    for key in ("_challenge_type", "_challenge_url"):
                        if key in approved_request.arguments:
                            bound_arguments[key] = approved_request.arguments[key]
                    if (approved_request.tool_name != agent_tool_name or approved_request.tool_version != tool.mcp_version
                        or approved_request.user_id != context.get("user_id")
                        or approved_request.run_id != context.get("run_id")
                        or approved_request.task_id != context.get("task_id")
                        or approved_request.arguments != bound_arguments):
                        approved_request = None
                if approved_request:
                    if isinstance(approved_request, dict):
                        from backend.models.security import ApprovalRequest
                        approved_request = ApprovalRequest(**approved_request)
                    self.approvals_engine.validate_approval_for_execution(approved_request, bound_arguments)
                else:
                    pending_approval = self.approvals_engine.create_approval_request(
                        user_id=context.get("user_id", "system"),
                        tool_name=agent_tool_name,
                        tool_version=tool.mcp_version,
                        risk_level=tool.risk_level,
                        autonomy_level=autonomy_level,
                        arguments=arguments,
                        workflow_id=context.get("workflow_id", "unknown"),
                        run_id=context.get("run_id", "unknown"),
                        task_id=context.get("task_id", "unknown"),
                    )
                    raise ApprovalRequiredException("Human approval is required for this action.", pending_approval)

        # §24.5 (R2): generated STANDALONE servers execute ONLY through
        # the isolated runner contract, gated by deployment eligibility
        # on CURRENT bytes — never through the adapter runtime or an
        # unisolated path.
        if (isinstance(raw_tool, dict) and
                raw_tool.get("artifact_type") == "standalone_server") or mcp.artifact_type == "standalone_server":
            return await self._execute_standalone_tool(
                mcp_raw, mcp, tool, arguments, context)

        transport = mcp.transport.value if hasattr(mcp.transport, "value") else str(mcp.transport)
        if transport == "internal" or (isinstance(raw_tool, dict) and raw_tool.get("operation")):
            try:
                return await execute_openapi_tool(
                    mcp.model_dump(mode="json"),
                    raw_tool if isinstance(raw_tool, dict) else tool.model_dump(mode="json"),
                    arguments,
                    user_id=context.get("user_id", "system"),
                    secrets_repo=self.secrets_repo,
                    run_id=context.get("run_id"),
                    task_id=context.get("task_id"),
                    emit_progress=context.get("emit_progress"),
                    emit_browser_frame=context.get("emit_browser_frame"),
                    approval_ctx={
                        **(context or {}),
                        "user_id": context.get("user_id", "system"),
                        "workflow_id": context.get("workflow_id"),
                        "run_id": context.get("run_id"),
                        "task_id": context.get("task_id"),
                    },
                )
            except Exception as pause:
                from backend.services.auth_challenges import ChallengePause
                if not isinstance(pause, ChallengePause):
                    raise
                engine = self.approvals_engine or ApprovalsEngine()
                pending_approval = engine.create_approval_request(
                    user_id=context.get("user_id", "system"),
                    tool_name=agent_tool_name,
                    tool_version=tool.mcp_version,
                    risk_level=RiskLevel.HIGH,
                    autonomy_level=context.get("autonomy_level", AutonomyLevel.L2_SEMI_AUTONOMOUS),
                    arguments={
                        **(arguments or {}),
                        "_challenge_type": pause.challenge_type,
                        "_challenge_url": pause.url,
                    },
                    workflow_id=context.get("workflow_id", "unknown"),
                    run_id=context.get("run_id", "unknown"),
                    task_id=context.get("task_id", "unknown"),
                )
                raise ApprovalRequiredException(pause.message, pending_approval) from pause

        try:
            from opentelemetry.propagate import inject
            trace_headers = {}
            inject(trace_headers)
            return await MCPClientManager.call_tool(mcp, tool_name, arguments, extra_headers=trace_headers)
        except Exception as e:
            logger.error("MCP execution failed: %s", e)
            raise ToolRouterError(f"MCP execution failed: {e}")

    async def _execute_standalone_tool(self, mcp_raw, mcp, tool,
                                       arguments, context) -> Any:
        """§24.5: the production dispatch for generated standalone
        servers — deployment-eligibility gate on CURRENT bytes, then the
        isolated runner contract. Refusals carry the gate reasons."""
        import hashlib
        import json as _json
        from backend.services.mcp_deployment_service import (
            evaluate_deployment_eligibility)
        from backend.services.mcp_protocol_validator import (
            REQUIRED_PROTOCOL_CASES, suite_digest)
        metadata = mcp_raw.get("metadata") or {}
        artifact = metadata.get("standalone_artifact") or {}
        attestations = metadata.get("standalone_attestations") or []
        if not artifact:
            raise ToolRouterError(
                "Standalone tool has no packaged artifact to verify")
        # CURRENT bytes: the digest is computed over the artifact's
        # server code as it exists NOW (metadata carries the code).
        content_digest_now = hashlib.sha256(
            artifact.get("server_code", "").encode()).hexdigest()
        if artifact.get("format") == "mcp_stdio_v1":
            from backend.mcp.builder.compute_server import artifact_digest
            content_digest_now = artifact_digest(artifact)
        from backend.services.isolated_runner import ExecutionRequest, RunnerUnavailableError, get_runner
        runner = get_runner()
        runner_profile = getattr(runner, "profile_digest", None)
        operation_policy = self.policy.is_allowed(tool, mcp)

        gate = evaluate_deployment_eligibility(
            artifact=artifact,
            attestations=attestations,
            owner_user_id=mcp_raw.get("owner", ""),
            caller_user_id=(context or {}).get("user_id", ""),
            connector_enabled=mcp.is_enabled,
            connector_revoked=bool(mcp_raw.get("revoked")),
            operation_eligibility=[{"tool_name": tool.tool_name,
                                    "allowed": operation_policy.allowed}],
            isolation_profile_verified=bool(runner_profile and getattr(runner, "image", None)
                                            == artifact.get("runtime_image_digest")),
            content_digest_now=content_digest_now,
            expected_runner_profile_digest=runner_profile or None)
        if not gate.get("allowed"):
            raise ToolRouterError(
                "Standalone tool deployment gate refused: "
                + ", ".join(gate.get("blocked_reasons", [])))
        if artifact.get("format") == "mcp_stdio_v1":
            from backend.services.standalone_protocol import invoke_artifact
            packaged = next((entry for entry in artifact.get("tools", []) if entry["name"] == tool.tool_name), None)
            if packaged is None or packaged["inputSchema"] != tool.input_schema:
                raise ToolRouterError("Standalone cached tool differs from its validated artifact")
            try:
                return await invoke_artifact(artifact, context["user_id"], tool.tool_name, arguments)
            except (ValueError, RunnerUnavailableError) as exc:
                raise ToolRouterError(str(exc)) from exc
        execution_id = (f"std-{(context or {}).get('run_id', 'run')[:16]}-"
                        f"{tool.tool_name}-{uuid.uuid4().hex[:8]}")
        try:
            request = ExecutionRequest(
                execution_id=execution_id,
                tenant_id=(context or {}).get("user_id", ""),
                artifact_digest=artifact.get("content_digest", ""),
                runtime_image_digest=artifact.get("runtime_image_digest"),
                policy_profile_id="protocol_test",
                operation_allowlist=[tool.tool_name],
                deadline_seconds=60)
            record = await runner.submit_execution(request)
            # Run the artifact's entrypoint: the packaged server's
            # self-test + the requested tool invocation.
            script = self._standalone_invocation_script(artifact, tool,
                                                        arguments)
            record = await runner.run_to_completion(
                execution_id, script,
                files={"server.py": artifact.get("server_code", ""),
                       "input.json": _json.dumps(arguments or {}),
                       "tool.json": _json.dumps({"tool": tool.tool_name}),
                       "run_tool.py": _RUNTOOL_SOURCE})
            outputs = await runner.collect_outputs(execution_id)
            await runner.cleanup_execution(execution_id)
            out = outputs[0] if outputs else {}
            if record.state.value != "completed":
                raise ToolRouterError(
                    f"Standalone tool execution failed "
                    f"({record.exit_reason}): {out.get('stderr_tail', '')[:300]}")
            return {"executed": True, "artifact_digest":
                    artifact.get("content_digest"),
                    "execution_id": execution_id,
                    "stdout": out.get("stdout_tail", ""),
                    "exit_code": out.get("exit_code", 0)}
        except RunnerUnavailableError as exc:
            raise ToolRouterError(
                f"Managed execution unavailable: {exc}") from exc

    @staticmethod
    def _standalone_invocation_script(artifact, tool, arguments) -> str:
        """The verified artifact's OWN entrypoint — the router never
        writes shell logic, it only drives the packaged server's
        protocol contract via the run_tool.py driver file."""
        return "cd /work && python run_tool.py\n"

    async def execute_tool_safe(self, agent_tool_name: str, arguments: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Any:
        if "__" not in agent_tool_name:
            raise ToolRouterError(f"Invalid tool name format: {agent_tool_name}")
        mcp_id, _tool_name = agent_tool_name.split("__", 1)
        manifest = await self.mcp_repo.get_mcp(mcp_id)
        user_id = (context or {}).get("user_id")
        if not manifest or not user_id or manifest.get("owner") not in (user_id, "system"):
            raise ToolRouterError("Integration is not accessible to this user")
        status = circuit_breaker.get_status(mcp_id)
        if status.get("state") == "OPEN":
            raise ToolRouterError("BLOCKED_UPSTREAM_OUTAGE")

        async def _execute():
            output = await self.execute_tool(agent_tool_name, arguments, context)
            if isinstance(output, dict) and (
                output.get("ok") is False or output.get("success") is False
                or output.get("isError") is True or output.get("status") in ("error", "failed")
            ):
                raise ToolRouterError(str(output.get("error") or output.get("message") or "The integration reported an unsuccessful execution"))
            return output

        workflow_id = context.get("workflow_id", "unknown") if context else "unknown"
        task_id = context.get("task_id", "unknown") if context else "unknown"

        try:
            if self.idempotency_guard and workflow_id != "unknown" and task_id != "unknown":
                result = await self.idempotency_guard.execute_once(
                    workflow_id=workflow_id,
                    task_id=task_id,
                    tool_name=agent_tool_name,
                    arguments=arguments,
                    execute_fn=_execute,
                    retry_safe=False,
                )
                if result.blocked:
                    raise ToolRouterError(f"Idempotency blocked execution: {result.reason}")
                output = result.result
            else:
                output = await _execute()
            circuit_breaker.record_success(mcp_id)
            return output
        except ApprovalRequiredException:
            raise
        except Exception:
            circuit_breaker.record_failure(mcp_id)
            raise


# The verified artifact's driver: reads the tool name and arguments from
# the input bundle and invokes the packaged server module. The ROUTER
# never writes shell logic — this driver only bridges the artifact's own
# module contract to the runner's file protocol.
_RUNTOOL_SOURCE = """
import json, sys
sys.path.insert(0, "/work")
import server  # the packaged standalone server module
tool = json.load(open("tool.json"))["tool"]
args = json.load(open("input.json"))
result = server.invoke_tool(tool, args)
print("TOOL_RESULT=" + json.dumps(result))
"""
