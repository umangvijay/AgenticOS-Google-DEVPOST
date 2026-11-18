"""Explicit connector artifact products (audit G03).

The product boundary the audit requires, made concrete:

- **adapter** (the default and only auto-activated product): a validated
  declarative OpenAPI mapping executed by the shared policy-controlled
  runtime. No generated code exists.
- **standalone_server** (explicit export product): a generated server
  package with a dependency lock. Packaging NEVER marks it deployable —
  `mark_deployable` refuses until initialize, tools/list and tools/call
  protocol tests have all passed in the supported runtime.

Nothing in the build pipeline calls the packaging path automatically:
custom-code/export is an explicit user action, per the audit.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ARTIFACT_ADAPTER = "adapter"
ARTIFACT_STANDALONE = "standalone_server"

# Protocol checks a standalone artifact must pass before it may be labeled
# deployable (audit G03 acceptance: initialize, list tools, validated calls).
REQUIRED_PROTOCOL_TESTS = ("initialize", "tools_list", "tools_call")


def manifest_digest(manifest: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, default=str).encode()
    ).hexdigest()


def content_hash(artifact: Dict[str, Any]) -> str:
    """Hash of the IMMUTABLE EXECUTABLE CONTENT ONLY (server code, tool
    schemas, dependency lock) — deliberately excluding test evidence and
    deployability flags, so recorded protocol tests can be bound to the
    exact content they validated (v5.6 A8)."""
    material = {
        "server_code": artifact.get("server_code"),
        "tools": artifact.get("tools"),
        "dependency_lock": artifact.get("dependency_lock"),
        "spec_provenance": artifact.get("spec_provenance"),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode()
    ).hexdigest()


def package_standalone_server(
    *,
    manifest: Dict[str, Any],
    tools: List[Dict[str, Any]],
    server_code: str,
    spec_provenance: str = "official_spec",
) -> Dict[str, Any]:
    """Produce an immutable standalone-server artifact: versioned source,
    a dependency lock, and per-tool schemas. Always ships `deployable:
    False` — deployment is gated by mark_deployable, never by packaging."""
    artifact = {
        "artifact_type": ARTIFACT_STANDALONE,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest_digest": manifest_digest(manifest),
        "spec_provenance": spec_provenance,
        # Immutable copies: the artifact is frozen at packaging time.
        "tools": [{k: t.get(k) for k in ("tool_name", "description", "input_schema", "operation")}
                  for t in tools],
        "server_code": server_code,
        # Exact dependency resolution (v5.6 A8): ranges are not locks —
        # a rebuild with drifted dependencies would not be the same artifact.
        "dependency_lock": {
            "python": "3.11.16",
            "mcp": "1.16.0",
            "httpx": "0.27.2",
            "pydantic": "2.9.2",
        },
        "protocol_tests": {name: {"status": "not_run"} for name in REQUIRED_PROTOCOL_TESTS},
        "deployable": False,
        "not_deployable_reason": "Protocol tests have not run in a supported runtime",
    }
    artifact["content_hash"] = content_hash(artifact)
    artifact["artifact_hash"] = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, default=str).encode()
    ).hexdigest()
    return artifact


def record_protocol_test(artifact: Dict[str, Any], test: str, *, passed: bool, detail: str = "") -> Dict[str, Any]:
    if test not in REQUIRED_PROTOCOL_TESTS:
        raise ValueError(f"Unknown protocol test: {test}")
    # Each result is BOUND to the content it exercised (v5.6 A8): if the
    # server code changes afterwards, the binding breaks and the artifact
    # can no longer be labeled deployable without re-running the tests.
    updated = {**artifact, "protocol_tests": {**artifact["protocol_tests"], test: {
        "status": "passed" if passed else "failed", "detail": detail,
        "content_hash": content_hash(artifact),
        "tested_at": datetime.now(timezone.utc).isoformat(),
    }}}
    updated["content_hash"] = content_hash(updated)
    updated["artifact_hash"] = hashlib.sha256(
        json.dumps(updated, sort_keys=True, default=str).encode()
    ).hexdigest()
    return updated


def mark_deployable(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Flip deployable only when every protocol test passed AND the recorded
    evidence is bound to the artifact's CURRENT immutable content (v5.6 A8:
    editing server_code after testing invalidates the evidence)."""
    current = content_hash(artifact)
    missing = []
    stale = []
    for name in REQUIRED_PROTOCOL_TESTS:
        entry = artifact.get("protocol_tests", {}).get(name, {})
        if entry.get("status") != "passed":
            missing.append(name)
        elif entry.get("content_hash") != current:
            stale.append(name)
    if missing or stale:
        reasons = []
        if missing:
            reasons.append("protocol tests not passed: " + ", ".join(missing))
        if stale:
            reasons.append("evidence is bound to different content (artifact changed after testing): " + ", ".join(stale))
        raise ValueError("Standalone server is not deployable: " + "; ".join(reasons))
    return {**artifact, "deployable": True, "not_deployable_reason": None}
