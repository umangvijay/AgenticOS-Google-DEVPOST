"""Controlled feedback → failing generated tool → correction → verified use.

This is product integration evidence. It is not a real-customer pilot receipt.
Only the HTTP identity is injected; builds, validation, execution and feedback
storage are the ordinary application paths.
"""
import asyncio
from contextlib import suppress
import json
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.tests.test_phase26_connector_journey import stack  # noqa: F401
from backend.tests.test_phase26_standalone_journey import managed_runner, specification  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", ["postgres", "firestore"], indirect=True)
async def test_feedback_links_observed_failure_to_independently_verified_correction(stack, managed_runner):
    f, engine, uid, _, _, _, _ = stack
    from backend.services.connector_build_service import request_durable_build, ORIGIN_ORCHESTRATOR
    from backend.worker import start_worker
    values = [int(uuid.uuid4().hex[:5], 16), 11]
    async def run_build(source, expected_status):
        run_id = "feedback-" + uuid.uuid4().hex
        await f.workflow_repo.save_run({"run_id": run_id, "user_id": uid, "goal": "Calculate the correct total",
            "status": "RUNNING", "tasks": [{"task_id": "total", "status": "WAITING", "agent": "core.connector_call",
                "input_data": {"operation": "ledger_total", "arguments": {"entries": values}}, "max_retries": 0}]})
        build = await request_durable_build(f, user_id=uid, method="python_tools", source=json.dumps(source),
            name="Ledger correction", origin=ORIGIN_ORCHESTRATOR, parent_run_id=run_id,
            parent_task_id="total", required_operations=["ledger_total"])
        await f.workflow_repo.update_task(build["run_id"], "mcp-build", {"recovery_enabled": False, "max_retries": 0})
        async with asyncio.timeout(40):
            while (run := await f.workflow_repo.get_run(run_id))["status"] not in ("FAILED", "COMPLETED"):
                await asyncio.sleep(.05)
        assert run["status"] == expected_status, run
        return run, await f.mcp_repo.get_build(build["build_id"])
    from backend.api.routers.feedback import router
    from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
    app = FastAPI(version="phase26-controlled-fix")
    app.state.factory = f
    role = {"value": "user"}
    async def identity():
        return AuthenticatedUser(uid, "fixture@example.test", "Fixture", role["value"], "local")
    app.dependency_overrides[get_current_user] = identity
    app.include_router(router)
    worker = asyncio.create_task(start_worker(f.message_bus, engine, user_repo=f.user_repo,
                                             org_repo=f.org_repo, mcp_repo=f.mcp_repo))
    try:
        source = specification()
        broken = {**source, "module": source["module"].replace("sum(entries)", "-999")}
        failed, rejected = await run_build(broken, "FAILED")
        assert not (await f.mcp_repo.get_mcp(rejected["mcp_id"]))["is_enabled"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture") as client:
            response = await client.post("/feedback", json={"helpful": False, "category": "incorrect_result",
                "comment": "Generated ledger computation fails its expected-result check",
                "expected_result": str(sum(values)), "run_id": failed["run_id"], "share_diagnostics": True})
            assert response.status_code == 201, response.text
            feedback_id = response.json()["feedback_id"]
            corrected, build = await run_build(source, "COMPLETED")
            output = corrected["tasks"][0]["output_data"]
            if isinstance(output, str):
                output = json.loads(output)
            assert output["structuredContent"] == {"total": sum(values)}
            active = await f.mcp_repo.get_mcp(build["mcp_id"])
            assert build["mcp_id"] != rejected["mcp_id"]
            note = json.dumps({"failed_run": failed["run_id"], "corrected_run": corrected["run_id"],
                "rejected_connector": rejected["mcp_id"], "active_revision": active["active_revision_id"],
                "artifact_digest": output["artifact_digest"], "execution_id": output["execution_id"],
                "independent_expected_total": sum(values), "observed_total": output["structuredContent"]["total"]})
            role["value"] = "admin"
            response = await client.post(f"/feedback/{feedback_id}/triage", json={"status": "fixed", "note": note})
            assert response.status_code == 200, response.text
            retained = await f.feedback_repo.get(feedback_id)
            assert retained["triage_status"] == "fixed"
            assert json.loads(retained["triage_note"])["corrected_run"] == corrected["run_id"]
            # §31.2: the journey's own product receipt — real run/build/
            # revision IDs the evidence producer consumes when the
            # harness requests observations.
            from backend.tests.journey_observations import emit
            emit("J17", {
                "run_id": corrected["run_id"],
                "build_id": build["build_id"],
                "revision": active["active_revision_id"],
                "artifact_id": output.get("artifact_digest"),
                "failed_run_id": failed["run_id"],
                "rejected_connector_id": rejected["mcp_id"],
                "independent_expected_total": sum(values),
                "observed_total": output["structuredContent"]["total"],
            })
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
