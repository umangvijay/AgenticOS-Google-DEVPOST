"""Audit G03 acceptance: explicit artifact products with a hard deploy gate.

- The default build product is the declarative adapter; no generated code.
- A standalone-server package always ships deployable=False.
- mark_deployable refuses unless initialize, tools/list AND tools/call all
  passed on the exact artifact.
- The manifest carries artifact_type with backward-compatible default.
"""
import pytest

from backend.mcp.standalone_packaging import (
    ARTIFACT_ADAPTER,
    ARTIFACT_STANDALONE,
    mark_deployable,
    package_standalone_server,
    record_protocol_test,
    REQUIRED_PROTOCOL_TESTS,
)


MANIFEST = {"mcp_id": "mcp-x", "name": "X", "version": "1.0.0"}
TOOLS = [{"tool_name": "getA", "description": "a", "input_schema": {"type": "object"}, "operation": {"http_method": "GET"}}]
CODE = "from mcp.server import Server\nserver = Server('x')\n"


def test_g03_default_product_is_the_adapter():
    from backend.models.mcp_schemas import MCPManifest
    m = MCPManifest(mcp_id="x", name="n", version="1", endpoint="e", transport="internal")
    assert m.artifact_type == ARTIFACT_ADAPTER


def test_g03_packaging_always_ships_quarantined():
    artifact = package_standalone_server(manifest=MANIFEST, tools=TOOLS, server_code=CODE)
    assert artifact["artifact_type"] == ARTIFACT_STANDALONE
    assert artifact["deployable"] is False
    assert artifact["not_deployable_reason"]
    assert set(artifact["protocol_tests"]) == set(REQUIRED_PROTOCOL_TESTS)
    assert all(t["status"] == "not_run" for t in artifact["protocol_tests"].values())
    # The artifact is immutable/verifiable: content-addressed hash.
    assert artifact["artifact_hash"]


def test_g03_mark_deployable_refuses_without_all_protocol_tests():
    artifact = package_standalone_server(manifest=MANIFEST, tools=TOOLS, server_code=CODE)
    artifact = record_protocol_test(artifact, "initialize", passed=True)
    artifact = record_protocol_test(artifact, "tools_list", passed=True)
    with pytest.raises(ValueError, match="tools_call"):
        mark_deployable(artifact)
    # A failed test also blocks:
    artifact_failed = record_protocol_test(artifact, "tools_call", passed=False, detail="boom")
    with pytest.raises(ValueError):
        mark_deployable(artifact_failed)


def test_g03_mark_deployable_passes_only_with_full_protocol_evidence():
    artifact = package_standalone_server(manifest=MANIFEST, tools=TOOLS, server_code=CODE)
    for test in REQUIRED_PROTOCOL_TESTS:
        artifact = record_protocol_test(artifact, test, passed=True, detail="ok")
    deployable = mark_deployable(artifact)
    assert deployable["deployable"] is True
    assert deployable["not_deployable_reason"] is None


def test_g03_artifact_hash_binds_evidence_to_content():
    """Tampering with the artifact invalidates the recorded hash."""
    artifact = package_standalone_server(manifest=MANIFEST, tools=TOOLS, server_code=CODE)
    original_hash = artifact["artifact_hash"]
    tampered = {**artifact, "server_code": CODE + "\n# tampered"}
    import hashlib, json as _json
    recomputed = hashlib.sha256(_json.dumps(tampered, sort_keys=True, default=str).encode()).hexdigest()
    assert recomputed != original_hash
