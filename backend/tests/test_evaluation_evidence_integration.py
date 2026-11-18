"""Phase 12 §12 integration: the evaluation adapter (evaluation_service.
evaluate_run_async — the worker's run-finish path) pulls REAL durable
evidence into the persisted record:

- effect-ledger states come from the effects table (uncertain outcomes
  fail the run honestly),
- execution steps supply tool-call counts, error-code accuracy rates,
  and latency,
- the record lands in the evaluation_records table and is readable by a
  FRESH repository instance (restart survival, end to end).
"""
import pytest
import pytest_asyncio

from backend.models.effect import Effect, EffectState, new_effect_id
from backend.models.schemas import Task, TaskStatus, WorkflowRun
from backend.repositories.effect_repository import (
    SQLiteEffectRepository, request_digest, set_effect_repo,
)
from backend.repositories.evaluation_repository import EvaluationRepository, set_evaluation_repo
from backend.repositories.execution_step_repository import (
    ExecutionStepRepository, set_execution_step_repo,
)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.evaluation_service import evaluate_run_async


@pytest_asyncio.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "eval-integration.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def wired(db):
    """Install all evidence repos as the process singletons — the same
    wiring the RepositoryFactory performs in production."""
    effect_repo = SQLiteEffectRepository(db)
    step_repo = ExecutionStepRepository(db)
    eval_repo = EvaluationRepository(db)
    set_effect_repo(effect_repo)
    set_execution_step_repo(step_repo)
    set_evaluation_repo(eval_repo)
    try:
        yield {"effect": effect_repo, "step": step_repo, "eval": eval_repo,
               "db": db}
    finally:
        set_effect_repo(None)
        set_execution_step_repo(None)
        set_evaluation_repo(None)


def _run_model(run_id: str) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id, workflow_id="wf-int", user_id="u1", goal="g",
        status=TaskStatus.COMPLETED,
        tasks=[
            Task(task_id="t1", workflow_id="wf-int", run_id=run_id,
                 user_id="u1", agent="core.set", status=TaskStatus.COMPLETED,
                 output_data={"reply": "Invoice sent to billing@x.io"},
                 acceptance_criteria=[{"kind": "contains",
                                       "value": "billing@x.io"}],
                 recovery_attempts=0),
            Task(task_id="t2", workflow_id="wf-int", run_id=run_id,
                 user_id="u1", agent="core.set", status=TaskStatus.COMPLETED,
                 output_data={"reply": "done"},
                 acceptance_criteria=[{"kind": "non_empty", "value": "reply"}],
                 recovery_attempts=1),
        ])


@pytest.mark.asyncio
async def test_uncertain_effect_evidence_fails_run_in_durable_record(wired):
    """An OUTCOME_UNKNOWN effect bound to the run is loaded from the
    effects table and honestly fails the persisted evaluation."""
    run = _run_model("run-ev1")
    fx, _ = await wired["effect"].prepare_effect(Effect(
        effect_id=new_effect_id(), operation_name="email.send",
        owner_user_id="u1", run_id="run-ev1", task_id="t1",
        request_digest=request_digest("u1", "email.send", {"r": ["b@x.io"]}),
        target="b@x.io"))
    # The state machine: effects start PREPARED; dispatch limbo is reached
    # through the CAS transition, exactly as a real dispatch would.
    await wired["effect"].transition(fx.effect_id, EffectState.DISPATCHING,
                                     actor="test")
    await wired["effect"].transition(fx.effect_id, EffectState.OUTCOME_UNKNOWN,
                                     actor="test")

    record = await evaluate_run_async(run)
    assert record.success is False, "uncertain effect must fail the run"

    # Restart survival: a FRESH repository instance over the same DB.
    fresh = EvaluationRepository(wired["db"])
    row = await fresh.get_evaluation(record.evaluation_id)
    assert row is not None
    assert row["success"] == 0, "uncertain-effect failure must be persisted"
    stored = row["record"]
    assert fx.effect_id in stored["uncertain_effects"]
    assert stored["run_id"] == "run-ev1"
    assert stored["evaluator_version"], "scorer version must travel with the record"


@pytest.mark.asyncio
async def test_execution_step_evidence_populates_tool_metrics(wired):
    """Tool-call counts and error-code accuracy rates come from the
    execution_steps table — not from in-memory history strings."""
    run = _run_model("run-ev2")
    # §12.15: tool calls = VALIDATED dispatch attempts, not every step.
    await wired["step"].create_step({
        "step_id": "step-ev2-1", "logical_action_id": "la-1",
        "run_id": "run-ev2", "task_id": "t1", "tenant_id": "u1",
        "sequence": 0, "validated_action_digest": "dig-1",
        "error_code": "UNKNOWN_TOOL_exception"})
    await wired["step"].create_step({
        "step_id": "step-ev2-2", "logical_action_id": "la-2",
        "run_id": "run-ev2", "task_id": "t2", "tenant_id": "u1",
        "sequence": 1, "validated_action_digest": "dig-2"})

    record = await evaluate_run_async(run)
    assert record.tool_calls == 2
    assert record.unknown_tool_call_rate == 0.5
    assert record.invalid_argument_rate == 0.0


@pytest.mark.asyncio
async def test_confirmed_effects_and_completed_tasks_succeed_durably(wired):
    run = _run_model("run-ev3")
    fx, _ = await wired["effect"].prepare_effect(Effect(
        effect_id=new_effect_id(), operation_name="email.send",
        owner_user_id="u1", run_id="run-ev3", task_id="t1",
        request_digest=request_digest("u1", "email.send", {"r": ["c@x.io"]}),
        target="c@x.io"))
    await wired["effect"].transition(fx.effect_id, EffectState.DISPATCHING,
                                     actor="test")
    await wired["effect"].transition(fx.effect_id, EffectState.CONFIRMED_APPLIED,
                                     actor="test")

    record = await evaluate_run_async(run)
    assert record.success is True

    fresh = EvaluationRepository(wired["db"])
    rows = await fresh.evaluations_for_run("run-ev3")
    assert len(rows) == 1
    assert rows[0]["record"]["confirmed_effects"]
    assert rows[0]["record"]["successful_recoveries"] == 1, \
        "t2 recovered once and completed"
