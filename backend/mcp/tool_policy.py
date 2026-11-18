from backend.models.mcp_schemas import MCPManifest, CachedToolDefinition, ToolPolicyResult
from backend.services.tool_validation import has_valid_read_probe, credential_binding_id_from

class ToolPolicy:
    def is_allowed(self, tool: CachedToolDefinition, mcp: MCPManifest, context: dict = None) -> ToolPolicyResult:
        if not mcp.is_enabled:
            return ToolPolicyResult(allowed=False, reason="MCP is disabled")
        if mcp.trust_tier != "verified":
            return ToolPolicyResult(allowed=False, reason="Connector revision requires validation")

        # Health is informational — intermittent upstream 5xx should not block catalog or execution.
        if tool.mcp_id != mcp.mcp_id:
            return ToolPolicyResult(allowed=False, reason="Tool does not belong to this MCP")

        # Phase 6 §6.7/§6.12: evidence binds to the credential CONTEXT
        # (ref+version+epoch), derived from the auth blob — not the raw ref.
        binding = credential_binding_id_from(
            mcp.auth.model_dump(mode="json") if hasattr(mcp.auth, "model_dump") else mcp.auth)
        # §24.5: standalone-server artifacts carry a STRICTER validation
        # chain — protocol attestations bound to the artifact's current
        # bytes, enforced by the deployment gate at dispatch — so the
        # adapter read-probe requirement does not apply to them.
        is_standalone = (mcp.artifact_type == "standalone_server"
                         if hasattr(mcp, "artifact_type")
                         else getattr(mcp, "legacy_metadata", {}) or {}
                         .get("artifact_type") == "standalone_server")
        if (mcp.transport == "internal" and not is_standalone
                and not has_valid_read_probe(
                    tool.model_dump(mode="json"), mcp.spec_hash, binding)):
            return ToolPolicyResult(allowed=False, reason="Operation needs validation for this revision and credential")

        return ToolPolicyResult(allowed=True)
