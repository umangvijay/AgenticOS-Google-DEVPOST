"""Factory → real stdio protocol → signed observation → parent tool execution.

The validator runs outside the workload. No attestation or completion is seeded.
The module supplied by the requester is converted to a standalone MCP artifact
by the production factory. This fixture qualifies network-disabled compute only.
"""
import asyncio
from contextlib import suppress
import hashlib
import json
import subprocess
import uuid

import pytest

from backend.tests.test_phase26_connector_journey import stack  # noqa: F401


@pytest.fixture
def managed_runner(tmp_path, monkeypatch):
    from backend.tests.test_phase24_standalone_dispatch import _runner_image_ready
    if not _runner_image_ready():
        pytest.skip("local isolated runner image required")
    from backend.services.runner_bridge import ContainerIsolatedRunner
    from backend.services.isolated_runner import RUNTIME_PROFILES
    from backend.runners.execution_store import ExecutionStore
    image = subprocess.check_output(["docker", "image", "inspect", "agentos-runner:latest", "--format", "{{.Id}}"], text=True).strip()
    profile = hashlib.sha256(json.dumps(RUNTIME_PROFILES, sort_keys=True).encode()).hexdigest()
    runner = ContainerIsolatedRunner(image=image, profile_digest=profile,
                                     record_store=ExecutionStore(tmp_path / "runner.db"))
    monkeypatch.setattr("backend.services.isolated_runner._runner", runner)
    yield runner
    runner.container.shutdown()
    runner.container._client.close()


def specification():
    values = [int(uuid.uuid4().hex[:5], 16), 37]
    return {"module": "def ledger_total(entries):\n    if not entries:\n        raise ValueError('No ledger entries')\n    return {'total': sum(entries)}\n",
        "tools": [{"name": "ledger_total", "description": "Total supplied ledger entries",
                   "inputSchema": {"type": "object", "properties": {"entries": {
                       "type": "array", "items": {"type": "number"}}},
                       "required": ["entries"], "additionalProperties": False}}],
        "validation": {"ledger_total": {"valid_arguments": {"entries": values},
            "expected": {"total": sum(values)}, "invalid_arguments": {"entries": "wrong type"},
            "error_arguments": {"entries": []}}}}


@pytest.mark.asyncio
async def test_python_build_api_requires_writer_and_uses_durable_admission(stack, managed_runner):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from backend.api.routers.integrations import router
    from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
    f, _, uid, _, _, _, _ = stack
    app = FastAPI()
    app.state.factory = f
    role = {"value": "viewer"}
    async def identity():
        return AuthenticatedUser(uid, "fixture@example.test", "Fixture", role["value"], "local")
    app.dependency_overrides[get_current_user] = identity
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture") as client:
        source = {**specification(), "name": "Requested compute MCP"}
        denied = await client.post("/integrations/build-python-tools", json=source)
        assert denied.status_code == 403
        role["value"] = "user"
        accepted = await client.post("/integrations/build-python-tools", json=source)
        assert accepted.status_code == 202, accepted.text
        build = await f.mcp_repo.get_build(accepted.json()["build_id"])
        assert build["required_operations"] == ["ledger_total"]
        assert build["user_id"] == uid and build["method"] == "python_tools"
        duplicate = await client.post("/integrations/build-python-tools", json=source)
        assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_isolated_server_reads_only_through_broker(managed_runner):
    from backend.mcp.builder.compute_server import generate
    from backend.services.standalone_protocol import validate_artifact
    source = {
        "module": "from agentos_http import get\nimport socket\ndef inspect_page(url):\n    response = get(url)\n    blocked = False\n    try:\n        connection = socket.create_connection(('93.184.216.34', 443), timeout=0.5)\n        connection.close()\n    except OSError:\n        blocked = True\n    return {'status': response['status_code'], 'has_title': 'Example Domain' in response['body'], 'direct_network_blocked': blocked}\n",
        "tools": [{"name": "inspect_page", "description": "Inspect a granted public page",
            "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}},
                            "required": ["url"], "additionalProperties": False}}],
        "read_grants": ["https://example.com"],
        "validation": {"inspect_page": {"valid_arguments": {"url": "https://example.com/"},
            "expected": {"status": 200, "has_title": True, "direct_network_blocked": True},
            "invalid_arguments": {"url": 123},
            "error_arguments": {"url": "https://169.254.169.254/latest/meta-data/"}}}}
    artifact = generate(source, managed_runner.image)
    validation = await validate_artifact(artifact, "owner", source["validation"])
    assert validation["status"] == "passed"
    observations = validation["observed"]["broker_observations"]
    assert len(observations) == 2
    assert observations[0]["outcome"] == "completed" and observations[0]["host"] == "example.com"
    assert observations[1]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_protocol_validator_observes_actual_container_exchanges(managed_runner):
    from backend.mcp.builder.compute_server import generate
    from backend.services.standalone_protocol import validate_artifact, invoke_artifact
    request = specification()
    artifact = generate(request, managed_runner.image)
    result = await validate_artifact(artifact, "owner", request["validation"])
    assert result["status"] == "passed"
    assert len(result["case_results"]) == 7
    assert result["observed"]["shutdown_observed"]
    assert result["observed"]["exchanges"]["ledger_total"]["tool_error"]["result"]["isError"]
    fresh = [int(uuid.uuid4().hex[:6], 16), -19]
    output = await invoke_artifact(artifact, "owner", "ledger_total", {"entries": fresh})
    assert output["structuredContent"] == {"total": sum(fresh)}
    # §31.2: the standalone-forge journey's own product receipt — the
    # generated artifact's digest and the real isolated execution ID.
    from backend.tests.journey_observations import emit
    emit("J03", {
        "artifact_sha256": hashlib.sha256(
            artifact if isinstance(artifact, bytes)
            else json.dumps(artifact, default=str).encode()).hexdigest(),
        "execution_id": output.get("execution_id"),
        "independent_output": output["structuredContent"],
        "protocol_cases_passed": len(result["case_results"]),
        "shutdown_observed": result["observed"]["shutdown_observed"],
    })
    assert await managed_runner.cleanup_execution(output["execution_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", ["postgres", "firestore"], indirect=True)
async def test_rebuild_does_not_overwrite_active_tool_and_disable_wins(stack, managed_runner, monkeypatch):
    from backend.services import standalone_build
    f, _, uid, _, _, _, router = stack
    source = json.dumps(specification())
    original = await standalone_build.build_compute_connector(f.mcp_repo, user_id=uid,
        source=source, name="Original", build_id=None)
    candidates = []
    real_validator = standalone_build.validate_artifact

    async def validate_then_user_disables(*args, **kwargs):
        result = await real_validator(*args, **kwargs)
        candidates.extend(m["mcp_id"] for m in await f.mcp_repo.list_mcps(uid)
                          if m["mcp_id"] != original["mcp_id"])
        assert len(candidates) == 1
        await f.mcp_repo.set_mcp_enabled(candidates[0], False)
        return result

    monkeypatch.setattr(standalone_build, "validate_artifact", validate_then_user_disables)
    with pytest.raises(ValueError, match="publication refused"):
        await standalone_build.build_compute_connector(f.mcp_repo, user_id=uid,
            source=source, name="Rebuild", build_id=None)
    assert not (await f.mcp_repo.get_mcp(candidates[0]))["is_enabled"]
    assert (await f.mcp_repo.get_mcp(original["mcp_id"]))["is_enabled"]
    result = await router.execute_tool(original["mcp_id"] + "__ledger_total",
        {"entries": [137, -42]}, context={"user_id": uid})
    assert result["structuredContent"] == {"total": 95}


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["false_result", "stdout_claim", "output_flood"])
async def test_validator_never_accepts_workload_claims_or_wrong_results(managed_runner, fault):
    from backend.mcp.builder.compute_server import generate
    from backend.services.standalone_protocol import validate_artifact
    request = specification()
    if fault == "false_result":
        request["module"] = request["module"].replace("sum(entries)", "-999")
    elif fault == "stdout_claim":
        request["module"] = "print('all protocol tests passed', flush=True)\n" + request["module"]
    else:
        request["module"] = "print('x' * 300000, flush=True)\n" + request["module"]
    artifact = generate(request, managed_runner.image)
    with pytest.raises(ValueError, match="MCP execution/shutdown failed"):
        await validate_artifact(artifact, "owner", request["validation"])
    records = list(managed_runner.container._records.values())
    assert records and all(record.cleanup_state == "cleaned" for record in records)
    assert all(record.exit_code != 0 for record in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", ["postgres", "firestore"], indirect=True)
async def test_worker_builds_and_uses_generated_stdio_server(stack, managed_runner):
    f, engine, uid, _, _, _, router = stack
    parent_id = "stdio-parent-" + uuid.uuid4().hex
    values = [int(uuid.uuid4().hex[:6], 16), -23, 1.25]
    await f.workflow_repo.save_run({"run_id": parent_id, "user_id": uid, "goal": "Total actual ledger values",
        "status": "RUNNING", "tasks": [{"task_id": "calculate", "status": "WAITING",
            "agent": "core.connector_call", "input_data": {"operation": "ledger_total", "arguments": {"entries": values}},
            "max_retries": 0, "recovery_enabled": False}]})
    from backend.services.connector_build_service import request_durable_build, ORIGIN_ORCHESTRATOR
    tracking = await request_durable_build(f, user_id=uid, method="python_tools", source=json.dumps(specification()),
        name="Generated ledger MCP", origin=ORIGIN_ORCHESTRATOR, parent_run_id=parent_id,
        parent_task_id="calculate", required_operations=["ledger_total"])
    from backend.worker import start_worker
    worker = asyncio.create_task(start_worker(f.message_bus, engine, user_repo=f.user_repo,
                                             org_repo=f.org_repo, mcp_repo=f.mcp_repo))
    try:
        async with asyncio.timeout(40):
            while (parent := await f.workflow_repo.get_run(parent_id))["status"] != "COMPLETED":
                child = await f.workflow_repo.get_run(tracking["run_id"])
                assert child["status"] != "FAILED", child
                assert parent["status"] != "FAILED", parent
                await asyncio.sleep(.1)
        output = parent["tasks"][0]["output_data"]
        if isinstance(output, str):
            output = json.loads(output)
        assert output["structuredContent"] == {"total": sum(values)}
        build = await f.mcp_repo.get_build(tracking["build_id"])
        manifest = await f.mcp_repo.get_mcp(build["mcp_id"])
        assert manifest["active_revision_id"]
        assert manifest["artifact_type"] == "standalone_server"
        evidence = manifest["metadata"]["protocol_evidence"]
        assert evidence["status"] == "passed" and evidence["execution_id"] != output["execution_id"]
        wait = await f.wait_repo.latest_wait(parent_id, "calculate")
        assert wait["state"] == "resolved"
        # The same production entrypoint must refuse altered executable bytes.
        manifest["metadata"]["standalone_artifact"]["files"]["implementation.py"] += "\n# altered\n"
        await f.mcp_repo.register_mcp(manifest)
        from backend.mcp.tool_router import ToolRouterError
        with pytest.raises(ToolRouterError, match="ARTIFACT_CONTENT_CHANGED"):
            await router.execute_tool(build["mcp_id"] + "__ledger_total", {"entries": values}, context={"user_id": uid})
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", ["postgres", "firestore"], indirect=True)
async def test_cancelled_workflow_stops_its_active_generated_server(stack, managed_runner):
    f, engine, uid, _, _, _, _ = stack
    spec = specification()
    spec["module"] = "import time\n" + spec["module"].replace(
        "    return {'total': sum(entries)}", "    time.sleep(20)\n    return {'total': sum(entries)}")
    from backend.services.connector_build_service import request_durable_build
    tracking = await request_durable_build(f, user_id=uid, method="python_tools", source=json.dumps(spec),
        name="Cancellable compute", required_operations=["ledger_total"])
    from backend.worker import start_worker
    worker = asyncio.create_task(start_worker(f.message_bus, engine, user_repo=f.user_repo,
                                             org_repo=f.org_repo, mcp_repo=f.mcp_repo))
    try:
        async with asyncio.timeout(15):
            while not managed_runner.container._containers:
                await asyncio.sleep(.05)
            execution_id = next(iter(managed_runner.container._containers))
            durable = await managed_runner.get_execution(execution_id)
            assert durable.request.workflow_run_id == tracking["run_id"]
            assert durable.request.workflow_task_id == "mcp-build"
            assert durable.request.workflow_attempt == 1
            await f.workflow_repo.update_run_status(tracking["run_id"], "CANCELLED")
            while not await managed_runner.cleanup_execution(execution_id):
                await asyncio.sleep(.05)
        durable = await managed_runner.get_execution(execution_id)
        assert durable.state.value == "cancelled"
        assert managed_runner.container._containers == {}
        build = await f.mcp_repo.get_build(tracking["build_id"])
        if build.get("mcp_id"):
            assert not (await f.mcp_repo.get_mcp(build["mcp_id"]))["is_enabled"]
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
