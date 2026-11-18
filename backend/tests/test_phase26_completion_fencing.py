"""Late work cannot overwrite cancellation or a newer execution attempt."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.models.schemas import Task, WorkflowRun
from backend.tests.test_phase26_controller_contract import store  # noqa: F401
from backend.tests.test_phase13_chat1 import stack, _save_run  # noqa: F401


@pytest.mark.asyncio
async def test_completion_requires_live_attempt_and_uncancelled_run(store):
    if type(store).__name__.startswith("Postgres"):
        from backend.repositories.postgres.workflow_repository import PostgresWorkflowEngineRepository
        repo = PostgresWorkflowEngineRepository(store.db)
    elif type(store).__name__.startswith("Firestore"):
        from backend.repositories.firestore.workflow_repository import FirestoreWorkflowRepository
        repo = FirestoreWorkflowRepository()
    else:
        from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
        repo = SQLiteWorkflowRepository(store.db)
    run = WorkflowRun(run_id="run", workflow_id="workflow", user_id="owner", goal="Return current output",
        status="RUNNING", tasks=[Task(task_id="task", run_id="run", workflow_id="workflow", user_id="owner",
                                     agent="core.set", input_data={}, status="PENDING")])
    await repo.save_run(run.model_dump(mode="json"))
    assert await repo.claim_task("run", "task", 60)
    # Expire attempt 1 and acquire attempt 2 through the same repository path
    # used by recovery, while the old execution still has a result to commit.
    expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    await repo.update_task("run", "task", {"lease_expires_at": expired})
    assert await repo.claim_task("run", "task", 60)
    completion = {"status": "COMPLETED", "output_data": {"value": "current"}}
    assert not await repo.update_task_if_not_cancelled("run", "task", completion, expected_attempt=1)
    current = (await repo.get_run("run"))["tasks"][0]
    assert current["status"] == "RUNNING" and current["attempt"] == 2
    # Cancel the run without changing the running task. The run itself is
    # authoritative even if task-level cancellation has not propagated yet.
    await repo.update_run_status("run", "CANCELLED")
    assert not await repo.update_task_if_not_cancelled("run", "task", completion, expected_attempt=2)
    assert (await repo.get_run("run"))["tasks"][0]["status"] == "RUNNING"
    await repo.update_run_status("run", "RUNNING")
    assert await repo.update_task_if_not_cancelled("run", "task", completion, expected_attempt=2)
    assert (await repo.get_run("run"))["tasks"][0]["output_data"] == {"value": "current"}
    assert not await repo.update_task_if_not_cancelled("run", "task", completion, expected_attempt=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_failure", [False, True])
async def test_cancelled_execution_cannot_publish_verified_step(stack, monkeypatch, late_failure):
    repo, engine = stack["repo"], stack["engine"]
    run = await _save_run(repo, "cancel-before-commit")
    execute = engine._execute_core_node

    async def cancel_after_execution(task, inputs, current_run):
        output = await execute(task, inputs, current_run)
        await repo.cancel_run(run.run_id)
        if late_failure:
            raise RuntimeError("Late tool failure after cancellation")
        return output

    monkeypatch.setattr(engine, "_execute_core_node", cancel_after_execution)
    await engine.execute_task(run.run_id, "t1")
    current = await repo.get_run(run.run_id)
    assert current["status"] == "CANCELLED"
    assert current["tasks"][0]["status"] in ("RUNNING", "CANCELLED"), \
        "neither late success nor late failure may overwrite the cancelled execution"
    steps = await stack["steps"].steps_for_run(run.run_id)
    assert steps and all(step["state"] != "VERIFIED" for step in steps)
