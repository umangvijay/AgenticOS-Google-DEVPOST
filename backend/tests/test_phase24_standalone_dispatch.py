"""Phase 24 §24.5 — the production-path standalone-tool journey (R2).

Unlike the flagship reconciliation test (which stages the child result),
THIS test drives the PRODUCTION dispatch: a packaged standalone server
registered with current attestations, invoked through ToolRouter
.execute_tool, gated on CURRENT bytes, executed by the real container
runner, and the result verified independently.

Failure cases on the SAME path: tampered bytes, missing attestation,
revoked connector, runner unavailable.

Requires the agentos-runner image (docker); skips loudly otherwise.
"""
import uuid

import pytest


def _runner_image_ready() -> bool:
    import os
    import docker
    client = None
    try:
        client = docker.from_env()
    except Exception:
        for cand in (os.path.expanduser("~/.colima/default/docker.sock"),
                     os.path.expanduser(
                         "~/.colima/_lima/colima/docker.sock")):
            if os.path.exists(cand):
                client = docker.DockerClient(base_url=f"unix://{cand}")
                break
    if client is None:
        return False
    try:
        client.images.get("agentos-runner:latest")
        return True
    except Exception:
        return False


def _server_code() -> str:
    """The generated standalone server: the artifact UNDER TEST. Its
    module contract: invoke_tool(tool_name, args) -> result."""
    return (
        "import json\n"
        "\n"
        "def invoke_tool(tool, args):\n"
        "    if tool == 'ledger_total':\n"
        "        return {'total': sum(args.get('entries', [])),\n"
        "                'tool': tool}\n"
        "    raise ValueError(f'unknown tool {tool}')\n"
    )


def _artifact() -> dict:
    import hashlib
    code = _server_code()
    return {
        "content_digest": hashlib.sha256(code.encode()).hexdigest(),
        "runtime_image_digest": "agentos-runner:latest",
        "protocol_validation_status": "passed",
        "isolation_validation_status": "passed",
        "server_code": code,
        "deployable": True,
    }


def _attestations(artifact: dict) -> list:
    from backend.services.artifact_attestation_service import issue_attestation
    from backend.services.mcp_protocol_validator import (
        REQUIRED_PROTOCOL_CASES, suite_digest)
    return [issue_attestation(
        artifact_digest=artifact["content_digest"],
        runtime_image_digest=artifact["runtime_image_digest"],
        runner_profile_digest="profile-current",
        test_suite_digest=suite_digest(),
        test_run_id=f"run-{uuid.uuid4().hex[:8]}",
        issuer_identity="trusted-validator",
        observed_results={c["case"]: "passed"
                          for c in REQUIRED_PROTOCOL_CASES})]


def _mcp_raw(artifact: dict, attestations: list, *, owner="owner-u",
             enabled=True, revoked=False) -> dict:
    return {
        "id": f"std-{uuid.uuid4().hex[:10]}",
        "name": "Ledger standalone",
        "version": "v1",
        "endpoint": "internal://standalone/dispatch",
        "transport": "internal",
        # The tool policy requires the verified trust tier (validation
        # evidence); the deployment gate separately binds CURRENT bytes.
        "trust_tier": "verified",
        "owner": owner,
        "enabled": enabled,
        "revoked": revoked,
        "artifact_type": "standalone_server",
        "metadata": {
            "standalone_artifact": artifact,
            "standalone_attestations": attestations,
            "runner_profile_digest": "profile-current",
        },
    }


def _cached_tool(tool_name: str, mcp_id: str = "") -> dict:
    return {"tool_name": tool_name, "name": tool_name, "mcp_id": mcp_id,
            "description": "sum ledger entries",
            "input_schema": {"type": "object", "properties": {
                "entries": {"type": "array"}}},
            "mcp_version": "1.0.0"}


def _router(mcp_raw, tool_name="ledger_total"):
    from backend.mcp.tool_router import ToolRouter

    class _McpRepo:
        def __init__(self):
            self._mcp_raw = mcp_raw

        async def get_mcp(self, mcp_id):
            return mcp_raw if mcp_raw.get("id") == mcp_id else None

        async def get_cached_tools(self, mcp_id):
            return [_cached_tool(tool_name, mcp_id)]

    return ToolRouter(mcp_repo=_McpRepo(), policy=__import__(
        "backend.mcp.tool_policy", fromlist=["ToolPolicy"]).ToolPolicy())


@pytest.mark.skipif(not _runner_image_ready(),
                    reason="BLOCKED: agentos-runner image unavailable — "
                           "bash scripts/build_runner_image.sh")
@pytest.mark.asyncio
async def test_production_dispatch_executes_generated_server_in_isolation(tmp_path, monkeypatch):
    """THE R2 production-path acceptance: goal-side tool invocation →
    gate (current bytes) → REAL container execution → independently
    verified result."""
    artifact = _artifact()
    import subprocess
    from backend.services import isolated_runner
    from backend.services.runner_bridge import ContainerIsolatedRunner
    from backend.runners.execution_store import ExecutionStore
    image = subprocess.check_output(["docker", "image", "inspect", "agentos-runner:latest",
                                     "--format", "{{.Id}}"], text=True).strip()
    artifact["runtime_image_digest"] = image
    runner = ContainerIsolatedRunner(image=image, profile_digest="profile-current",
                                     record_store=ExecutionStore(tmp_path / "journal.db"))
    monkeypatch.setattr(isolated_runner, "_runner", runner)
    router = _router(_mcp_raw(artifact, _attestations(artifact)))
    mcp_id = router.mcp_repo._mcp_raw["id"]
    result = await router.execute_tool(
        f"{mcp_id}__ledger_total",
        {"entries": [10, 20, 12]},
        context={"user_id": "owner-u", "run_id": f"run-{uuid.uuid4().hex[:8]}"})
    # Independent verification: the container's actual computation.
    assert result["executed"] is True
    assert "TOOL_RESULT=" in result["stdout"]
    payload = result["stdout"].split("TOOL_RESULT=", 1)[1].splitlines()[0]
    import json
    parsed = json.loads(payload)
    assert parsed["total"] == 42, parsed
    assert parsed["tool"] == "ledger_total"


@pytest.mark.asyncio
async def test_tampered_bytes_are_refused_by_the_gate():
    """The artifact's bytes changed after validation → the gate must
    refuse through the SAME production path."""
    artifact = _artifact()
    raw = _mcp_raw(artifact, _attestations(artifact))
    raw["metadata"]["standalone_artifact"]["server_code"] += (
        "\n# TAMPERED AFTER VALIDATION\n")
    router = _router(raw)
    from backend.mcp.tool_router import ToolRouterError
    with pytest.raises(ToolRouterError) as exc:
        await router.execute_tool(
            f"{raw['id']}__ledger_total", {"entries": [1]},
            context={"user_id": "owner-u", "run_id": "run-x"})
    assert "ARTIFACT_CONTENT_CHANGED" in str(exc.value)


@pytest.mark.asyncio
async def test_missing_attestation_is_refused():
    artifact = _artifact()
    raw = _mcp_raw(artifact, [])  # no attestations
    router = _router(raw)
    from backend.mcp.tool_router import ToolRouterError
    with pytest.raises(ToolRouterError) as exc:
        await router.execute_tool(
            f"{raw['id']}__ledger_total", {"entries": [1]},
            context={"user_id": "owner-u", "run_id": "run-x"})
    assert "NO_TRUSTED_ATTESTATION" in str(exc.value)


@pytest.mark.asyncio
async def test_revoked_connector_is_refused():
    artifact = _artifact()
    raw = _mcp_raw(artifact, _attestations(artifact), revoked=True)
    router = _router(raw)
    from backend.mcp.tool_router import ToolRouterError
    with pytest.raises(ToolRouterError):
        await router.execute_tool(
            f"{raw['id']}__ledger_total", {"entries": [1]},
            context={"user_id": "owner-u", "run_id": "run-x"})


@pytest.mark.asyncio
async def test_owner_mismatch_is_refused():
    artifact = _artifact()
    raw = _mcp_raw(artifact, _attestations(artifact), owner="someone-else")
    router = _router(raw)
    from backend.mcp.tool_router import ToolRouterError
    with pytest.raises(ToolRouterError) as exc:
        await router.execute_tool(
            f"{raw['id']}__ledger_total", {"entries": [1]},
            context={"user_id": "owner-u", "run_id": "run-x"})
    # The router's ownership layer denies before the gate fires — an
    # equally valid refusal on the same path.
    assert "not accessible" in str(exc.value) or \
        "CALLER_NOT_AUTHORIZED" in str(exc.value)
