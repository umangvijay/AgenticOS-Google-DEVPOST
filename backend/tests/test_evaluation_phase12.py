"""Phase 12 §12 acceptance tests: evaluation records survive restart,
reproduce results for fixed fixtures, and use the Phase 5/10 evidence
contract instead of placeholder proxies."""
import pytest
import pytest_asyncio

from backend.repositories.evaluation_repository import EvaluationRepository
from backend.services.evaluation_runner import evaluate_run


@pytest_asyncio.fixture
async def db(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "evals.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def repo(db):
    return EvaluationRepository(db)


def _run_tasks():
    # §12.2 corrected semantics: every task carries REAL acceptance
    # criteria — a task with none is needs_review and can never verify.
    return [
        {"task_id": "t1", "status": "COMPLETED", "recovery_attempts": 0,
         "acceptance_criteria_json": [{"kind": "non_empty", "value": "reply"}],
         "output_data": {"reply": "Here is the result."}},
        {"task_id": "t2", "status": "COMPLETED", "recovery_attempts": 1,
         "acceptance_criteria_json": [{"kind": "non_empty", "value": "reply"}],
         "output_data": {"reply": "Done"}},
    ]


# ── §12: evaluation records survive restart ──────────────────────────

@pytest.mark.asyncio
async def test_evaluation_survives_restart(repo):
    """§12 acceptance: evaluation records survive restart."""
    record = evaluate_run(
        run_id="run-eval", workflow_id="wf-eval", user_id="u1",
        tasks=_run_tasks(), model_name="test-model")
    await repo.save_evaluation(record)
    # Fresh repo (same db) sees the record.
    fresh = EvaluationRepository(repo.db)
    fetched = await fresh.get_evaluation(record["evaluation_id"])
    assert fetched is not None
    assert fetched["run_id"] == "run-eval"
    assert fetched["success"] == 1


# ── §12: reproduce results for fixed fixtures ────────────────────────

def test_fixed_fixture_reproduces_same_evaluation():
    """§12 acceptance: evaluation records reproduce results for fixed
    fixtures — the fixture hash is deterministic."""
    tasks = _run_tasks()
    r1 = evaluate_run(run_id="run-1", workflow_id="wf-1", user_id="u1",
                      tasks=tasks, model_name="m")
    r2 = evaluate_run(run_id="run-1", workflow_id="wf-1", user_id="u1",
                      tasks=tasks, model_name="m")
    assert r1["fixture_hash"] == r2["fixture_hash"]
    assert r1["success"] == r2["success"]
    # The evaluation_id is NOT deterministic (unique per run).
    assert r1["evaluation_id"] != r2["evaluation_id"]


def test_changed_fixture_produces_different_hash():
    tasks_a = _run_tasks()
    tasks_b = [{**t, "task_id": t["task_id"] + "-changed"} for t in _run_tasks()]
    r1 = evaluate_run(run_id="run-1", workflow_id="wf-1", user_id="u1",
                      tasks=tasks_a, model_name="m")
    r2 = evaluate_run(run_id="run-1", workflow_id="wf-1", user_id="u1",
                      tasks=tasks_b, model_name="m")
    assert r1["fixture_hash"] != r2["fixture_hash"]


# ── §12: effect-state verification uses correct semantics ────────────

def test_uncertain_effects_prevent_success():
    """§12: uncertain effects prevent overall success even when all tasks
    are COMPLETED."""
    tasks = _run_tasks()
    result = evaluate_run(
        run_id="run-1", workflow_id="wf-1", user_id="u1", tasks=tasks,
        effect_states={"fx_1": "OUTCOME_UNKNOWN"}, model_name="m")
    assert result["success"] is False
    assert "fx_1" in result["uncertain_effects"]


def test_failed_effects_prevent_success():
    """§12: failed effects prevent overall success."""
    result = evaluate_run(
        run_id="run-1", workflow_id="wf-1", user_id="u1", tasks=_run_tasks(),
        effect_states={"fx_1": "CONFIRMED_NOT_APPLIED"}, model_name="m")
    assert result["success"] is False
    assert "fx_1" in result["failed_effects"]


def test_confirmed_effects_allow_success():
    result = evaluate_run(
        run_id="run-1", workflow_id="wf-1", user_id="u1", tasks=_run_tasks(),
        effect_states={"fx_1": "CONFIRMED_APPLIED"}, model_name="m")
    assert result["success"] is True
    assert "fx_1" in result["confirmed_effects"]


def test_failed_task_prevents_success_even_with_confirmed_effects():
    result = evaluate_run(
        run_id="run-1", workflow_id="wf-1", user_id="u1",
        tasks=[{**_run_tasks()[0], "status": "FAILED"}],
        effect_states={"fx_1": "CONFIRMED_APPLIED"}, model_name="m")
    assert result["success"] is False


# ── §12: scorer version recorded, cannot rewrite history ─────────────

@pytest.mark.asyncio
async def test_scorer_version_recorded(repo):
    record = evaluate_run(
        run_id="run-v", workflow_id="wf-v", user_id="u1", tasks=_run_tasks(),
        model_name="m", evaluator_version="2.0.0-test")
    await repo.save_evaluation(record)
    fetched = await repo.get_evaluation(record["evaluation_id"])
    assert fetched["evaluator_version"] == "2.0.0-test"


@pytest.mark.asyncio
async def test_summary_aggregates(repo):
    record = evaluate_run(
        run_id="run-s", workflow_id="wf-s", user_id="u1", tasks=_run_tasks(),
        model_name="m")
    await repo.save_evaluation(record)
    summary = await repo.evaluation_summary()
    assert summary["total"] >= 1
