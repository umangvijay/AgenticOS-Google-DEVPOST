"""Phase 20 §20.18: durable ReAct restart recovery — a restart resumes
from durable state WITHOUT duplicating effects. The evidence: a NEW
engine instance over the SAME database (the definition of restart),
effect anchors preventing second dispatches (the dispatch callable is
counted), and deferral timers that survive the process boundary."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.config.settings import settings
from backend.engine.engine import WorkflowEngine
from backend.models.effect import Effect, EffectState
from backend.models.schemas import Task, TaskStatus, WorkflowRun
from backend.repositories.in_memory_message_bus import InMemoryMessageBus
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.effect_repository import SQLiteEffectRepository
from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
from backend.services.effect_service import dispatch_effect


@pytest_asyncio.fixture
async def stack(tmp_path):
    db_path = tmp_path / "react.db"
    db = DatabaseManager(str(db_path))
    await db.initialize()
    repo = SQLiteWorkflowRepository(db)
    engine = WorkflowEngine(repo, InMemoryMessageBus())
    effects = SQLiteEffectRepository(db)
    yield {"db_path": db_path, "db": db, "repo": repo, "engine": engine,
           "effects": effects}
    await db.close()


def _new_engine(stack) -> WorkflowEngine:
    """A NEW engine over the SAME durable repo — a process restart."""
    return WorkflowEngine(stack["repo"], InMemoryMessageBus())


async def _save_run(repo, run_id: str) -> WorkflowRun:
    task = Task(task_id="t1", workflow_id="wf-react", run_id=run_id,
                user_id="u1", agent="core.set",
                input_data={"fields": {"ok": True}},
                status=TaskStatus.PENDING)
    run = WorkflowRun(run_id=run_id, workflow_id="wf-react", user_id="u1",
                      goal="react restart drill", status=TaskStatus.RUNNING,
                      tasks=[task])
    await repo.save_run(run.model_dump(mode="json"))
    return run


@pytest.mark.asyncio
async def test_restart_resumes_stranded_task_to_completion(stack):
    """Worker claims → 'crashes'. A NEW engine instance (restart) picks
    the task up after the lease expires and COMPLETES it — attempt 2,
    not a fresh start."""
    repo, engine = stack["repo"], stack["engine"]
    run_id = f"react-restart-{uuid.uuid4().hex[:6]}"
    run = await _save_run(repo, run_id)

    assert await repo.claim_task(run_id, "t1", lease_seconds=60)
    stranded = (await repo.get_run(run_id))["tasks"][0]
    assert stranded["status"] == TaskStatus.RUNNING, "crash left it RUNNING"

    # Restart: lease long expired; the new engine reclaims and executes.
    past = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    await repo.update_task(run_id, "t1", {"lease_expires_at": past})
    restarted_engine = _new_engine(stack)
    await restarted_engine.execute_task(run_id, "t1")
    final = (await repo.get_run(run_id))["tasks"][0]
    assert final["status"] == TaskStatus.COMPLETED
    assert final["attempt"] == 2, "resumed, not restarted from scratch"


@pytest.mark.asyncio
async def test_restart_never_duplicates_a_committed_effect(stack):
    """THE §20.18 acceptance: an effect whose PREPARE or DISPATCHING
    commit survived the crash is deduplicated on restart — the external
    dispatch callable runs AT MOST ONCE across the restart."""
    effects = stack["effects"]
    uid = "u1"
    digest_args = {"to": "victim@example.com", "subject": "restart drill"}
    from backend.models.effect import EffectDispatchResult, EffectOutcome
    from backend.repositories.effect_repository import request_digest
    digest = request_digest(uid, "email.send", digest_args)

    def _result(*, outcome: EffectOutcome, effect_id: str) -> EffectDispatchResult:
        return EffectDispatchResult(
            outcome=outcome, effect_id=effect_id,
            retry_disposition="none", reconciliation_required=False,
            safe_user_message="sent")
    dispatch_calls = {"count": 0}

    async def dispatch(effect, attempt_id):
        dispatch_calls["count"] += 1
        return {"recipients": ["victim@example.com"], "accepted": True}

    def classify(result, effect_id):
        from backend.models.effect import EffectOutcome
        return _result(outcome=EffectOutcome.CONFIRMED_APPLIED,
                       effect_id=effect_id)

    run_id = f"react-fx-{uuid.uuid4().hex[:6]}"
    # First attempt: prepare + DISPATCHING committed, then the process
    # dies before the provider call resolves (§5.6 crash boundary).
    first = await dispatch_effect(
        effect_repo=effects, operation_name="email.send",
        owner_user_id=uid, run_id=run_id, task_id="t1",
        request_digest=digest, semantic_args=digest_args,
        target="victim@example.com", provider="smtp.test",
        dispatch=dispatch, classify=classify)
    assert dispatch_calls["count"] == 1

    # RESTART: the same semantic request arrives again (the task resumed).
    resumed = await dispatch_effect(
        effect_repo=effects, operation_name="email.send",
        owner_user_id=uid, run_id=run_id, task_id="t1",
        request_digest=digest, semantic_args=digest_args,
        target="victim@example.com", provider="smtp.test",
        dispatch=dispatch, classify=classify)
    assert dispatch_calls["count"] == 1, \
        "the restart did NOT re-dispatch the committed effect"
    assert resumed.effect_id == first.effect_id, \
        "the SAME effect identity is returned after restart"
    assert resumed.retry_disposition == "none"


@pytest.mark.asyncio
async def test_deferral_timer_survives_restart(stack):
    """§4.11 (C9): deferral lands on the next_attempt_at COLUMN — a new
    engine finds the due deferred task after restart."""
    repo = stack["repo"]
    run_id = f"react-defer-{uuid.uuid4().hex[:6]}"
    await _save_run(repo, run_id)
    due_iso = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    await repo.update_task_deferral(run_id, "t1", due_iso, defer_count=2)
    # A NEW engine (restart) queries the durable column.
    due = await repo.find_due_deferred_tasks(
        datetime.now(timezone.utc).isoformat(), limit=50)
    assert any(t["task_id"] == "t1" and t["run_id"] == run_id for t in due), \
        "the deferred task is discoverable after restart"
    task = await repo.get_task(run_id, "t1")
    assert task["defer_count"] == 2


@pytest.mark.asyncio
async def test_retrying_task_with_elapsed_timer_is_woken_by_the_sweep(stack):
    """§32.6: a RETRYING task whose durable retry timer elapsed (the
    in-process fast-path trigger was lost) is woken by the reconciler's
    due-deferral sweep and completes through a fresh claim — it is
    never stranded in RETRYING while its run stays RUNNING forever."""
    repo, engine = stack["repo"], stack["engine"]
    run_id = f"react-retry-{uuid.uuid4().hex[:6]}"
    await _save_run(repo, run_id)
    # The retry path durably persisted the timer, then the process lost
    # the fast-path trigger: the task is RETRYING with an elapsed timer.
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    await repo.update_task_deferral(run_id, "t1", past, defer_count=1)
    await repo.update_task(run_id, "t1", {"status": TaskStatus.RETRYING})
    from backend.services.command_reconciler import _wake_due_deferrals
    woken = await _wake_due_deferrals(repo, engine.message_bus, 50)
    assert woken >= 1, "the sweep did not wake the due RETRYING task"
    # The wake leads to a fresh claim (CAS accepts RETRYING) + execution.
    await engine.execute_task(run_id, "t1")
    final = (await repo.get_run(run_id))["tasks"][0]
    assert final["status"] == TaskStatus.COMPLETED, \
        "the woken retry did not complete"
    assert final["attempt"] == 1


@pytest.mark.asyncio
async def test_duplicate_delivery_after_restart_executes_once(stack):
    """At-least-once redelivery across a restart: the SAME task trigger
    delivered twice (post-restart) yields ONE completion, attempt == 1."""
    repo = stack["repo"]
    run_id = f"react-dup-{uuid.uuid4().hex[:6]}"
    await _save_run(repo, run_id)
    engine_a = stack["engine"]           # the pre-restart worker
    engine_b = _new_engine(stack)        # the post-restart worker
    await asyncio.gather(engine_a.execute_task(run_id, "t1"),
                         engine_b.execute_task(run_id, "t1"))
    task = (await repo.get_run(run_id))["tasks"][0]
    assert task["status"] == TaskStatus.COMPLETED
    assert task["attempt"] == 1, "duplicate delivery across restart is one execution"
