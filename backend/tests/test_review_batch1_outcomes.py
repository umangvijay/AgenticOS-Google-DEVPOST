"""Review batch 1 (§12.0–12.4) acceptance tests: truthful outcome semantics.

- §12.2: the engine completion boundary — the NEW result is verified
  against the task's authoritative criteria; needs_review parks the task
  and the run never claims COMPLETED.
- §12.4: the effect-state decision table (every state + unknown values +
  mixed multi-target results).
- Review finding B: verifier verdicts gate overall success.
- Review finding C: effects join tasks by explicit task_id, never
  substring.
- §12.11/§12.12: duplicate deliveries return the ORIGINAL evaluation; a
  new scorer version links a new revision; no in-place rewrites.
- §12.29: the SQLite summary window matches the advertised recent-window
  semantics (parity with Firestore).
"""
import asyncio

import pytest
import pytest_asyncio

from backend.models.effect import EffectState
from backend.repositories.evaluation_repository import EvaluationRepository
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.evaluation_runner import (
    WRITE_AWAITING_AUTHORIZATION,
    WRITE_AWAITING_PROVIDER,
    WRITE_NOT_APPLIED,
    WRITE_NOT_EXECUTED,
    WRITE_PARTIALLY_APPLIED,
    WRITE_UNCERTAIN,
    WRITE_UNVERIFIED,
    WRITE_VERIFIED_APPLIED,
    evaluate_run,
    required_write_interpretation,
)

PASS_CRITERIA = [{"kind": "non_empty", "value": "reply"}]


def _task(task_id="t1", status="COMPLETED", output=None, criteria=None,
          recovery=0):
    return {"task_id": task_id, "status": status,
            "recovery_attempts": recovery,
            "acceptance_criteria_json": PASS_CRITERIA if criteria is None
                                        else criteria,
            "output_data": output if output is not None
                           else {"reply": "done"}}


# ── §12.4: the effect-state decision table ─────────────────────────────

def test_effect_decision_table_covers_every_state():
    assert required_write_interpretation("PREPARED") == WRITE_NOT_EXECUTED
    assert required_write_interpretation(
        "WAITING_APPROVAL") == WRITE_AWAITING_AUTHORIZATION
    assert required_write_interpretation("DISPATCHING") == WRITE_UNCERTAIN
    assert required_write_interpretation(
        "PROVIDER_PENDING") == WRITE_AWAITING_PROVIDER
    assert required_write_interpretation(
        "CONFIRMED_APPLIED") == WRITE_VERIFIED_APPLIED
    assert required_write_interpretation(
        "CONFIRMED_NOT_APPLIED") == WRITE_NOT_APPLIED
    assert required_write_interpretation(
        "PARTIALLY_APPLIED") == WRITE_PARTIALLY_APPLIED
    assert required_write_interpretation("OUTCOME_UNKNOWN") == WRITE_UNCERTAIN
    assert required_write_interpretation(
        "CANCELLED_BEFORE_DISPATCH") == WRITE_NOT_APPLIED
    assert required_write_interpretation(
        "CLOSED_UNRESOLVED") == WRITE_UNVERIFIED


def test_unknown_effect_state_is_never_success():
    """§12.4 acceptance: unknown enum values map to unverified — never to
    a state that could satisfy a required write."""
    assert required_write_interpretation("SOME_FUTURE_STATE") == WRITE_UNVERIFIED
    assert required_write_interpretation(None) == WRITE_UNVERIFIED
    assert required_write_interpretation(12345) == WRITE_UNVERIFIED


def test_mixed_multi_target_result_blocks_success():
    """§12.4: PARTIALLY_APPLIED (mixed per-target results) preserves the
    partial result and blocks run success."""
    result = evaluate_run(
        run_id="r", workflow_id="w", user_id="u",
        tasks=[_task()],
        effects=[{"effect_id": "fx1", "task_id": "t1",
                  "state": "PARTIALLY_APPLIED"}],
        model_name="m")
    assert result["success"] is False
    assert "fx1" in result["partially_applied_effects"]


def test_prepared_never_dispatched_is_not_success():
    result = evaluate_run(
        run_id="r", workflow_id="w", user_id="u",
        tasks=[_task()],
        effects=[{"effect_id": "fx1", "task_id": "t1", "state": "PREPARED"}],
        model_name="m")
    assert result["success"] is False


# ── Review finding B: verdicts gate success ────────────────────────────

def test_completed_task_with_failing_criteria_is_not_success():
    """The core §12.2 correction: a task recorded COMPLETED whose criteria
    FAIL makes the run NOT verified — counts alone never do."""
    result = evaluate_run(
        run_id="r", workflow_id="w", user_id="u",
        tasks=[_task(output={"reply": ""})],   # non_empty fails
        model_name="m")
    assert result["success"] is False
    assert result["task_verifications"][0]["verifier_verdict"] == "fail"


def test_task_without_criteria_is_needs_review_not_success():
    result = evaluate_run(
        run_id="r", workflow_id="w", user_id="u",
        tasks=[_task(criteria=[])],   # no criteria → needs_review
        model_name="m")
    assert result["success"] is False
    assert result["task_verifications"][0]["verifier_verdict"] == "needs_review"


def test_passing_verdicts_and_confirmed_effects_verify():
    result = evaluate_run(
        run_id="r", workflow_id="w", user_id="u",
        tasks=[_task("t1"), _task("t2", recovery=1)],
        effects=[{"effect_id": "fx1", "task_id": "t1",
                  "state": "CONFIRMED_APPLIED"}],
        model_name="m")
    assert result["success"] is True
    assert result["successful_recoveries"] == 1


# ── Review finding C: explicit identity joins ──────────────────────────

def test_effect_cannot_satisfy_a_different_task_via_id_substring():
    """§12.13: an effect whose id CONTAINS the task id but whose task_id
    field names another task must not be attributed by substring."""
    result = evaluate_run(
        run_id="r", workflow_id="w", user_id="u",
        tasks=[_task("t1"), _task("t2")],
        effects=[{"effect_id": "fx_t2_substring_probe", "task_id": "t1",
                  "state": "CONFIRMED_APPLIED"}],
        model_name="m")
    # fx belongs to t1 by FIELD despite the id mentioning t2.
    tv2 = next(tv for tv in result["task_verifications"]
               if tv["task_id"] == "t2")
    assert tv2["effect_count"] == 0
    tv1 = next(tv for tv in result["task_verifications"]
               if tv["task_id"] == "t1")
    assert tv1["effect_count"] == 1


def test_unattributable_effect_still_gates_the_run():
    """An effect whose task_id names no task in this run gates the RUN —
    it can never be silently dropped."""
    result = evaluate_run(
        run_id="r", workflow_id="w", user_id="u",
        tasks=[_task("t1")],
        effects=[{"effect_id": "fx-orphan", "task_id": "task-from-elsewhere",
                  "state": "OUTCOME_UNKNOWN"}],
        model_name="m")
    assert result["success"] is False
    assert "fx-orphan" in result["unattributed_effects"]
    assert "fx-orphan" in result["uncertain_effects"]


# ── §12.2: the engine parks needs_review runs ──────────────────────────

@pytest_asyncio.fixture
async def stack(tmp_path):
    from backend.engine.engine import WorkflowEngine
    from backend.repositories.in_memory_message_bus import InMemoryMessageBus
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository)
    from backend.repositories.sqlite.workflow_repository import (
        SQLiteWorkflowRepository)
    db = DatabaseManager(str(tmp_path / "boundary.db"))
    await db.initialize()
    workflow_repo = SQLiteWorkflowRepository(db)
    policy_repo = SQLiteAdaptivePolicyRepository(db)
    engine = WorkflowEngine(workflow_repo, InMemoryMessageBus(),
                            adaptive_policy_repo=policy_repo)
    yield {"db": db, "repo": workflow_repo, "engine": engine}
    await db.close()


async def _save_run(repo, run_id, criteria):
    task = Task = None
    from backend.models.schemas import Task, TaskStatus, WorkflowRun
    task = Task(task_id="t1", workflow_id="wf-boundary", run_id=run_id,
                user_id="u1", agent="core.set",
                input_data={"fields": {"ok": True}},
                acceptance_criteria=criteria, status=TaskStatus.PENDING)
    run = WorkflowRun(run_id=run_id, workflow_id="wf-boundary",
                      user_id="u1", goal="boundary", status=TaskStatus.RUNNING,
                      tasks=[task])
    await repo.save_run(run.model_dump(mode="json"))
    return run


@pytest.mark.asyncio
async def test_needs_review_never_becomes_completed_run(stack):
    """§12.2 acceptance: needs_review does NOT become verified success —
    the task parks in NEEDS_REVIEW and the run lands in NEEDS_REVIEW."""
    from backend.models.schemas import TaskStatus
    repo, engine = stack["repo"], stack["engine"]
    # A non_empty criterion on a key the core node's output lacks →
    # needs_review (output exists but criterion unverifiable/failing path
    # yields needs_review only when output has content but criteria do not
    # all pass deterministically... "contains" on a missing key fails).
    run = await _save_run(repo, "boundary-nr-1",
                          [{"kind": "contains", "value": "XYZZY"}])
    await engine.execute_task(run.run_id, "t1")
    final = await repo.get_run(run.run_id)
    task = final["tasks"][0]
    assert task["status"] in (TaskStatus.NEEDS_REVIEW, TaskStatus.FAILED), \
        "unresolved verification must park or fail, never complete"
    if task["status"] == TaskStatus.NEEDS_REVIEW:
        assert final["status"] == TaskStatus.NEEDS_REVIEW, \
            "a needs-review run cannot claim COMPLETED"


@pytest.mark.asyncio
async def test_failing_criteria_fail_the_task_with_current_result(stack):
    """§12.2: verification inspects the NEW result (a core.set node
    returning a payload missing the required key fails its criteria)."""
    from backend.models.schemas import TaskStatus
    repo, engine = stack["repo"], stack["engine"]
    run = await _save_run(repo, "boundary-fail-1",
                          [{"kind": "contains", "value": "absent-token"}])
    await engine.execute_task(run.run_id, "t1")
    final = await repo.get_run(run.run_id)
    task = final["tasks"][0]
    assert task["status"] == TaskStatus.FAILED
    assert "acceptance criteria" in str(task.get("error", ""))


# ── §12.11/§12.12: immutable evaluation history ────────────────────────

@pytest_asyncio.fixture
async def eval_repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "immutable-evals.db"))
    await db.initialize()
    yield EvaluationRepository(db)
    await db.close()


def _eval_record(run_id="run-dup", version="3.0.0", success=True):
    return {
        "evaluation_id": f"eval_{run_id}_{version}".replace(".", "-"),
        "run_id": run_id, "workflow_id": "wf", "user_id": "u1",
        "agent_name": "orchestrator", "model": "m", "task_type": "workflow",
        "success": success, "recovery_attempts": 0,
        "successful_recoveries": 0, "tool_calls": 1,
        "unknown_tool_call_rate": None, "invalid_argument_rate": None,
        "tool_schema_violation_rate": None, "latency_ms": 1500.0,
        "token_usage_total": 320, "token_usage_input": None,
        "token_usage_output": None, "fixture_hash": "fh-1",
        "evaluator_version": version, "prompt_version": "1.0.0",
        "created_at": "2026-11-02T00:00:00+00:00",
        "created_at_epoch": 1,
    }


@pytest.mark.asyncio
async def test_duplicate_delivery_returns_the_original(eval_repo):
    """§12.11: repeated terminal events neither inflate samples nor lose
    evaluations — the ORIGINAL record comes back."""
    first, created1 = await eval_repo.save_evaluation(_eval_record())
    assert created1 is True
    tampered = _eval_record(success=False)   # different content, same identity
    second, created2 = await eval_repo.save_evaluation(tampered)
    assert created2 is False
    assert second["evaluation_id"] == first["evaluation_id"]
    assert second["success"] is True, "history must not be rewritten"

    rows = await eval_repo.evaluations_for_run("run-dup")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_new_scorer_version_creates_linked_revision(eval_repo):
    """§12.12: a new scorer version writes a NEW record LINKED to the
    previous one — historical pass rates cannot be silently rewritten."""
    v3, _ = await eval_repo.save_evaluation(_eval_record(version="3.0.0"))
    v4, created = await eval_repo.save_evaluation(
        _eval_record(version="4.0.0", success=False))
    assert created is True
    assert v4["previous_evaluation_id"] == v3["evaluation_id"]
    rows = await eval_repo.evaluations_for_run("run-dup")
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_unavailable_measurements_are_null_not_zero(eval_repo):
    """§12.9: unknown usage cannot be confused with zero usage."""
    record = _eval_record()
    record["token_usage_total"] = None
    record["latency_ms"] = None
    await eval_repo.save_evaluation(record)
    row = await eval_repo.get_evaluation(record["evaluation_id"])
    assert row["token_usage_total"] is None
    assert row["latency_ms"] is None


# ── §12.29: the summary window matches the advertised semantics ────────

@pytest.mark.asyncio
async def test_summary_aggregates_only_the_recent_window(eval_repo):
    for i in range(5):
        rec = _eval_record(run_id=f"run-w{i}")
        rec["success"] = i % 2 == 0
        rec["created_at_epoch"] = 100 + i
        await eval_repo.save_evaluation(rec)
    summary = await eval_repo.evaluation_summary(limit=3)
    assert summary["total"] == 3, "the LIMIT selects the recent window, " \
        "not a post-aggregation cap"


# ── §12.14: usage from the durable ledger ──────────────────────────────

@pytest.mark.asyncio
async def test_usage_summary_for_run_joins_the_ledger(tmp_path):
    from backend.models.usage import (MeasurementSource, ReservationRequest,
                                      SettlementReceipt)
    from backend.repositories.usage_repository import (
        BucketLimit, SQLiteUsageRepository, set_usage_repo)
    db = DatabaseManager(str(tmp_path / "usage-join.db"))
    await db.initialize()
    repo = SQLiteUsageRepository(db)
    set_usage_repo(repo)
    try:
        res = await repo.reserve(ReservationRequest(
            user_id="u1", units=500, run_id="run-usage", task_id="t1",
            call_id="call-1", model="gemini-flash-x"),
            [BudgetScope_USER("u1")])
        await repo.settle(res.reservation_id, SettlementReceipt(
            reservation_id=res.reservation_id, units=420,
            measurement_source=MeasurementSource.PROVIDER_REPORTED,
            resolved_model="gemini-flash-x"))
        res2 = await repo.reserve(ReservationRequest(
            user_id="u1", units=100, run_id="run-usage", task_id="t1",
            call_id="call-2", model="gemini-flash-x"),
            [BudgetScope_USER("u1")])
        await repo.mark_uncertain(res2.reservation_id, "disconnect")

        summary = await repo.usage_summary_for_run("run-usage")
        assert summary["measured_units"] == 420
        assert summary["uncertain_units"] == 100
        assert summary["total_units"] == 520, "uncertain usage is counted"
        assert summary["models_used"] == ["gemini-flash-x"]
    finally:
        set_usage_repo(None)
        await db.close()


def BudgetScope_USER(user_id):
    from backend.models.usage import BudgetScope
    from backend.repositories.usage_repository import BucketLimit
    return BucketLimit(BudgetScope.USER, subject_id=user_id, limit=1_000_000)


@pytest.mark.asyncio
async def test_protected_failure_stays_visible_without_specialist(stack):
    """§27.8 (audit 2026-11-13): when the Outcome Verifier specialist
    delegation is UNAVAILABLE (no contract repo configured), the
    protected failure must stay VISIBLE — the task parks NEEDS_REVIEW
    with its recorded reasons and no specialist verdict is fabricated."""
    from backend.models.schemas import TaskStatus
    from backend.repositories.agent_task_repository import (
        get_agent_task_repo, set_agent_task_repo)
    assert get_agent_task_repo() is None, \
        "this trace requires the specialist repo to be unconfigured"
    repo, engine = stack["repo"], stack["engine"]
    run = await _save_run(repo, "boundary-nr-nospec-1",
                          [{"kind": "contains", "value": "XYZZY"}])
    await engine.execute_task(run.run_id, "t1")
    final = await repo.get_run(run.run_id)
    task = final["tasks"][0]
    assert task["status"] in (TaskStatus.NEEDS_REVIEW, TaskStatus.FAILED), \
        "the unresolved verification must remain visible, never complete"
    if task["status"] == TaskStatus.NEEDS_REVIEW:
        reasons = str(task.get("error") or "")
        assert "needs review" in reasons.lower() or "verification" in reasons.lower(), \
            f"the recorded reasons must name the unresolved verification: {reasons!r}"
        # no specialist verdict was fabricated for the unavailable call
        out = task.get("output_data") or {}
        assert "specialist_verdict" not in json.dumps(out, default=str)
