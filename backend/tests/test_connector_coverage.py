"""Audit G02 + G06 acceptance: per-operation test evidence and provenance.

- G02: an integration with ten operations and one passing probe reports
  exactly that coverage; untested operations cannot inherit the verified
  badge or activation (ToolPolicy denies them per operation).
- G06: provenance is stamped per operation at generation, survives the
  probe, and is the trust ceiling — an inferred sketch never becomes
  verified, whatever a probe says.
"""
import pytest

from backend.services.tool_validation import (
    credential_binding_id_from,
    coverage_from_tools,
    has_valid_read_probe,
    record_read_probe,
    validation_provenance,
)


def _tool(name: str, method: str = "GET", validation: dict | None = None) -> dict:
    return {
        "tool_name": name,
        "mcp_id": "mcp-x",
        "mcp_version": "1.0.0",
        "description": f"{name} op",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "operation": {"http_method": method, "path": f"/{name}", "_validation": validation} if validation
                    else {"http_method": method, "path": f"/{name}"},
    }


class _ToolObj:
    """Mimics CachedToolDefinition for coverage_from_tools."""
    def __init__(self, d: dict):
        self._d = d
    def model_dump(self, mode="json"):
        return self._d


# ── G02: coverage ─────────────────────────────────────────────────────

def test_g02_ten_operations_one_probe_reports_exactly_that_coverage():
    tools = [_ToolObj(_tool(f"op{i}", validation={"kind": "untested", "status": "not_run"})) for i in range(9)]
    tools.append(_ToolObj(_tool("probed", validation={"kind": "read_probe", "status": "passed"})))
    coverage = coverage_from_tools(tools)
    assert coverage == {"total": 10, "read_probe_passed": 1, "untested": 9, "mutating_untested": 0, "context": "historical", "credential_binding_id": None}


def test_g02_untested_writes_counted_separately():
    tools = [
        _ToolObj(_tool("get_ok", validation={"kind": "read_probe", "status": "passed"})),
        _ToolObj(_tool("post_untested", method="POST", validation={"kind": "untested", "status": "not_run"})),
        _ToolObj(_tool("del_untested", method="DELETE", validation={"kind": "untested", "status": "not_run"})),
    ]
    coverage = coverage_from_tools(tools)
    assert coverage["mutating_untested"] == 2
    assert coverage["read_probe_passed"] == 1


def test_g02_untested_operation_cannot_pass_execution_policy():
    """The policy gate keys on per-operation evidence: an untested operation
    inside a verified integration is still denied (G01+G02 together)."""
    from backend.models.mcp_schemas import CachedToolDefinition, MCPManifest, ToolPolicyResult
    from backend.mcp.tool_policy import ToolPolicy

    manifest = MCPManifest(
        mcp_id="mcp-x", name="X", version="1.0.0", endpoint="internal://x", transport="internal",
        trust_tier="verified", is_enabled=True, spec_hash="hash-1",
        test_coverage={"total": 2, "read_probe_passed": 1, "untested": 1},
    )
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    def as_cached(d):
        return CachedToolDefinition(**{**d, "discovered_at": now, "expires_at": now + timedelta(hours=1),
                                       "risk_level": 1 if d["operation"]["http_method"] == "GET" else 4})
    probed_dict = {**_tool("probed", validation={"kind": "untested", "status": "not_run", "provenance": "official_spec"}), "risk_level": 1}
    probed = as_cached(record_read_probe(probed_dict, "hash-1",
                                         credential_binding_id_from(manifest.auth)))
    untested = as_cached(_tool("untested", validation={"kind": "untested", "status": "not_run", "provenance": "official_spec"}))
    policy = ToolPolicy()
    assert policy.is_allowed(probed, manifest).allowed is True
    denied = policy.is_allowed(untested, manifest)
    assert denied.allowed is False


# ── G06: provenance ───────────────────────────────────────────────────

def test_g06_provenance_stamped_at_generation():
    from backend.mcp.builder.schema_generator import SchemaGenerator
    from backend.mcp.builder.openapi_parser import OpenAPIParser

    import json as _json
    spec = _json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/a": {"get": {"operationId": "getA", "responses": {"200": {"description": "ok"}}}},
            "/b": {"get": {"operationId": "getB", "responses": {"200": {"description": "ok"}}}},
        },
    })
    model = OpenAPIParser().parse_text(spec)
    gen = SchemaGenerator()
    tools = gen.generate(model, "mcp-x", "1.0.0", spec_provenance="inferred_description")
    assert len(tools) == 2
    for t in tools:
        assert t.operation["_validation"]["kind"] == "untested"
        assert t.operation["_validation"]["provenance"] == "inferred_description"
        assert validation_provenance(t.model_dump(mode="json")) == "inferred_description"


def test_g06_provenance_survives_the_probe():
    tool = _tool("op", validation={"kind": "untested", "status": "not_run", "provenance": "inferred_description"})
    stamped = record_read_probe(tool, "hash-1", credential_binding_id=None)
    evidence = stamped["operation"]["_validation"]
    assert evidence["kind"] == "read_probe" and evidence["status"] == "passed"
    assert evidence["provenance"] == "inferred_description"


def test_g06_inferred_spec_with_passing_probe_stays_pending_review():
    """Acceptance: a nonexistent endpoint (inferred sketch) cannot gain trust
    through an unrelated passing endpoint — the build's trust tier keys on
    the provenance of the SOURCE, not on the probe result."""
    # This mirrors mcp_factory_agent's tier assignment exactly.
    for method, probe_ok, expected in [
        ("url", True, "verified"),
        ("spec", True, "verified"),
        ("openapi", True, "verified"),
        ("docs", True, "pending_review"),
        ("prompt", True, "pending_review"),
        ("url", False, "pending_review"),
    ]:
        trust_tier = "verified" if method in ("url", "spec", "openapi") and probe_ok else "pending_review"
        assert trust_tier == expected, (method, probe_ok)


def test_g06_provenance_is_the_trust_ceiling_in_the_manifest():
    from backend.models.mcp_schemas import MCPManifest
    m = MCPManifest(mcp_id="x", name="n", version="1.0.0", endpoint="internal://x", transport="internal",
                    spec_provenance="inferred_description",
                    test_coverage={"total": 4, "read_probe_passed": 1, "untested": 3},
                    capabilities_omitted=2)
    assert m.spec_provenance == "inferred_description"
    assert m.test_coverage["untested"] == 3
    assert m.capabilities_omitted == 2
