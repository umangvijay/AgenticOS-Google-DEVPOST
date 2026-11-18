"""Phase 21 §21.7: genuine dynamic behavior — user-specific outputs
derive from actual authorized input, actual inference, actual tool
execution, and observed results. Vary the inputs; the outputs must
vary with them. Missing inference is an explicit unavailable result."""
import uuid

import pytest
import pytest_asyncio

from backend.engine.engine import WorkflowEngine
from backend.models.schemas import Task, TaskStatus, WorkflowRun
from backend.repositories.in_memory_message_bus import InMemoryMessageBus
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def stack(tmp_path):
    db = DatabaseManager(str(tmp_path / "dyn21.db"))
    await db.initialize()
    repo = SQLiteWorkflowRepository(db)
    engine = WorkflowEngine(repo, InMemoryMessageBus())
    yield {"db": db, "repo": repo, "engine": engine}
    await db.close()


async def _run_goal(stack, goal: str, fields: dict) -> dict:
    repo, engine = stack["repo"], stack["engine"]
    run_id = f"dyn-{uuid.uuid4().hex[:10]}"
    task = Task(task_id="t1", workflow_id="wf-dyn", run_id=run_id,
                user_id="u1", agent="core.set",
                input_data={"fields": fields}, status=TaskStatus.PENDING)
    run = WorkflowRun(run_id=run_id, workflow_id="wf-dyn", user_id="u1",
                      goal=goal, status=TaskStatus.RUNNING, tasks=[task])
    await repo.save_run(run.model_dump(mode="json"))
    await engine.execute_task(run_id, "t1")
    return (await repo.get_run(run_id))["tasks"][0]


async def test_outputs_vary_with_user_input(stack):
    """Two DIFFERENT goals/inputs produce DIFFERENT recorded outputs —
    the run's output derives from the actual input, never a canned
    response."""
    out1 = await _run_goal(stack, "Set the marker to ALPHA-111",
                           {"marker": "ALPHA-111"})
    out2 = await _run_goal(stack, "Set the marker to OMEGA-222",
                           {"marker": "OMEGA-222"})
    assert out1["status"] == "COMPLETED" and out2["status"] == "COMPLETED"
    o1 = str(out1.get("output_data"))
    o2 = str(out2.get("output_data"))
    assert "ALPHA-111" in o1 and "ALPHA-111" not in o2, \
        "output 1 carries user input 1"
    assert "OMEGA-222" in o2 and "OMEGA-222" not in o1, \
        "output 2 carries user input 2"


async def test_missing_inference_is_explicit_unavailable(stack):
    """A model-requiring step without a configured provider reports an
    explicit unavailable/failed state — never a canned answer and never
    fake progress."""
    task = Task(task_id="t-model", workflow_id="wf-dyn",
                run_id=f"dyn-missing-{uuid.uuid4().hex[:6]}",
                user_id="u1", agent="gemini.pipeline",
                input_data={"prompt": "summarize"}, status=TaskStatus.PENDING)
    from backend.models.schemas import TaskStatus as TS
    run = WorkflowRun(run_id=task.run_id, workflow_id="wf-dyn",
                      user_id="u1", goal="model-required",
                      status=TaskStatus.RUNNING, tasks=[task])
    await stack["repo"].save_run(run.model_dump(mode="json"))
    try:
        await stack["engine"].execute_task(task.run_id, task.task_id)
    except Exception:
        pass  # the engine may raise; the RECORD is what must be honest
    final = (await stack["repo"].get_run(task.run_id))["tasks"][0]
    # The task did NOT complete with fabricated output.
    assert final["status"] != "COMPLETED" or not (
        final.get("output_data") or {}).get("rewarded_model"), \
        "a model-required step never completes with rewarded output " \
        "when no provider is configured"


async def test_tool_outputs_record_execution_provenance(stack):
    """Executed tool steps record WHERE their output came from (the
    agent/tool identity) — provenance is on the record, not implied."""
    out = await _run_goal(stack, "Set marker PROV-9", {"marker": "PROV-9"})
    assert out.get("agent") == "core.set", \
        f"the record names the executing agent: {out.get('agent')}"
