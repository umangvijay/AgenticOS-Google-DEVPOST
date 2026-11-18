import asyncio
import logging
from typing import Any, Dict, List, Optional
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession
from backend.models.mcp_schemas import MCPManifest, MCPTransportType, AuthType, CachedToolDefinition
from datetime import datetime, timezone, timedelta
import httpx

logger = logging.getLogger(__name__)


class MCPToolCallResult:
    """Phase 7 §7.17: the COMPLETE MCP tool result — content blocks,
    structured output, and the tool's own error status preserved. A provider
    tool error can no longer become an ordinary successful result merely
    because its text content was returned."""

    def __init__(self, *, content: List[Any], structured_content: Any = None,
                 is_error: bool = False, metadata: Optional[Dict[str, Any]] = None):
        self.content = content
        self.structured_content = structured_content
        self.is_error = is_error
        self.metadata = metadata or {}

    def text(self) -> str:
        """The concatenated text of text content blocks (back-compat view)."""
        parts = []
        for block in self.content or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": [
                {"type": getattr(b, "type", "unknown"),
                 "text": getattr(b, "text", None)}
                for b in (self.content or [])
            ],
            "structured_content": self.structured_content,
            "is_error": self.is_error,
            "metadata": self.metadata,
        }


def _preserve_result(result: Any) -> MCPToolCallResult:
    """§7.17: preserve the fields relevant to the supported SDK/protocol —
    field names verified against the installed dependency set via getattr
    with safe fallbacks (SDK releases have renamed these)."""
    content = getattr(result, "content", None) or []
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    is_error = bool(getattr(result, "isError", False)
                    or getattr(result, "is_error", False))
    meta = getattr(result, "meta", None)
    if meta is None:
        meta = getattr(result, "_meta", None)
    return MCPToolCallResult(content=list(content), structured_content=structured,
                             is_error=is_error, metadata=meta if isinstance(meta, dict) else {})


class MCPClientManager:
    @classmethod
    def get_auth_headers(cls, manifest: MCPManifest, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = extra_headers or {}
        return headers

    @classmethod
    async def list_tools(cls, manifest) -> List[CachedToolDefinition]:
        if isinstance(manifest, dict):
            from backend.mcp.tool_router import _as_manifest
            manifest = _as_manifest(manifest)
        return await cls.discover_tools(manifest)

    @classmethod
    async def discover_tools(cls, manifest: MCPManifest) -> List[CachedToolDefinition]:
        tools = []
        if manifest.transport == MCPTransportType.STREAMABLE_HTTP:
            headers = cls.get_auth_headers(manifest)
            client = httpx.AsyncClient(headers=headers)
            async with streamable_http_client(manifest.endpoint, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    # §7.17: complete pagination — list until no next cursor,
                    # so all advertised tools are discoverable.
                    result = await session.list_tools()
                    tools = list(result.tools)
                    next_cursor = getattr(result, "nextCursor", None)
                    while next_cursor:
                        result = await session.list_tools(cursor=next_cursor)
                        tools.extend(result.tools)
                        next_cursor = getattr(result, "nextCursor", None)
        elif manifest.transport == MCPTransportType.STDIO:
            from backend.mcp.sandbox.sandbox_controller import SandboxController
            from backend.mcp.sandbox.audit_logger import audit_logger
            import shlex
            parts = shlex.split(manifest.endpoint)
            base_cmd = parts[0] if parts else ""
            args = parts[1:] if len(parts) > 1 else []
            docker_cmd, docker_args = SandboxController.apply_docker_sandbox(
                manifest=manifest, command=base_cmd, args=args
            )
            server_params = StdioServerParameters(command=docker_cmd, args=docker_args)
            try:
                async with asyncio.timeout(15):
                    async with stdio_client(server_params) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            await session.initialize()
                            result = await session.list_tools()
                            tools = list(result.tools)
                            next_cursor = getattr(result, "nextCursor", None)
                            while next_cursor:
                                result = await session.list_tools(cursor=next_cursor)
                                tools.extend(result.tools)
                                next_cursor = getattr(result, "nextCursor", None)
            except asyncio.TimeoutError:
                audit_logger.log_sandbox_timeout(manifest.mcp_id, 15)
                raise RuntimeError(f"Sandbox Execution Timeout for {manifest.mcp_id}")
            except Exception as e:
                audit_logger.log_sandbox_violation(manifest.mcp_id, "EXECUTION_ERROR", str(e))
                raise
        elif manifest.transport == MCPTransportType.INTERNAL:
            return []
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=1)
        cached_tools = []
        for t in tools:
            cached_tools.append(CachedToolDefinition(
                tool_name=t.name,
                description=t.description,
                input_schema=t.input_schema,
                mcp_id=manifest.mcp_id,
                mcp_version=manifest.version,
                discovered_at=now,
                expires_at=expires_at,
            ))
        return cached_tools

    @classmethod
    async def call_tool(cls, manifest: MCPManifest, tool_name: str, arguments: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> MCPToolCallResult:
        """§7.17: returns the PRESERVED result (content + structured + error
        status). Callers that only need text use `.text()`; callers that must
        honor tool errors check `.is_error`."""
        if manifest.transport == MCPTransportType.STREAMABLE_HTTP:
            headers = cls.get_auth_headers(manifest, extra_headers)
            client = httpx.AsyncClient(headers=headers)
            async with streamable_http_client(manifest.endpoint, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    return _preserve_result(result)
        elif manifest.transport == MCPTransportType.STDIO:
            from backend.mcp.sandbox.sandbox_controller import SandboxController
            from backend.mcp.sandbox.audit_logger import audit_logger
            import shlex
            parts = shlex.split(manifest.endpoint)
            base_cmd = parts[0] if parts else ""
            args = parts[1:] if len(parts) > 1 else []
            docker_cmd, docker_args = SandboxController.apply_docker_sandbox(
                manifest=manifest, command=base_cmd, args=args
            )
            server_params = StdioServerParameters(command=docker_cmd, args=docker_args)
            try:
                async with asyncio.timeout(15):
                    async with stdio_client(server_params) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            await session.initialize()
                            result = await session.call_tool(tool_name, arguments)
                            return _preserve_result(result)
            except asyncio.TimeoutError:
                audit_logger.log_sandbox_timeout(manifest.mcp_id, 15)
                raise RuntimeError(f"Sandbox Execution Timeout calling {tool_name}")
            except Exception as e:
                audit_logger.log_sandbox_violation(manifest.mcp_id, "TOOL_EXECUTION_ERROR", str(e))
                raise e
        else:
            raise ValueError(f"Unknown transport {manifest.transport}")
