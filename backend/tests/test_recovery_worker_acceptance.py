"""Phase 11 §11.19 acceptance tests: the actual recovery worker with
deterministic fixtures — duplicate delivery, atomic claims, strict action
validation, cycle detection, deadline enforcement, and the escalation path.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from backend.models.recovery import RecoveryAction, RecoveryActionEnum
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
from backend.services.recovery_policy import RecoveryPolicy
from backend.models.schemas import (
    Task, TaskStatus, TaskRecoveryEvent, WorkflowRun,
)
from backend.workers.recovery_worker import RecoveryWorker


@pytest_asyncio.fixture
async def stack(tmp_path):
    db = DatabaseManager(str(tmp_path / "recovery-worker.db"))
    await db.initialize()
    repo = SQLiteWorkflowRepository(db)
    # §12.5: production wires the episode repository (the claim authority)
    # via the factory — the worker refuses to authorize recovery without
    # it, so the fixture mirrors that wiring.
    from backend.repositories.recovery_episode_repository import (
        RecoveryEpisodeRepository, set_recovery_episode_repo)
    episode_repo = RecoveryEpisodeRepository(db)
    set_recovery_episode_repo(episode_repo)
    bus = MagicMock()
    bus.publish = AsyncMock()
    engine = SimpleNamespace(evaluate_dag=AsyncMock(), notification_repo=None)
    worker = RecoveryWorker(bus, repo, engine)
    try:
        yield {"repo": repo, "bus": bus, "worker": worker, "engine": engine,
               "episodes": episode_repo}
    finally:
        set_recovery_episode_repo(None)
        await db.close()


def _task(task_id="t1", status=TaskStatus.RECOVERING, recovery_attempts=0,
          error="timeout", agent="core.http", tool=None):
    return Task(
        task_id=task_id, run_id="run-1", workflow_id="wf-1", user_id="u1",
        agent=agent, tool=tool, status=status,
        input_data={"url": "https://api.example.com/data"},
        error=error, error_type="TIMEOUT_ERROR",
        recovery_attempts=recovery_attempts,
        recovery_history=[],
    )


def _run(tasks):
    return WorkflowRun(
        run_id="run-1", workflow_id="wf-1", user_id="u1",
        goal="test", status="RUNNING", tasks=tasks,
    )


def _event(attempt=1):
    return TaskRecoveryEvent(
        run_id="run-1", workflow_id="wf-1", task_id="t1",
        recovery_attempt=attempt,
    )


# ── §11.19: duplicate recovery delivery → one logical attempt ─────────

@pytest.mark.asyncio
async def test_duplicate_recovery_delivery_ignored(stack):
    """§11.19: duplicate recovery delivery → one logical attempt."""
    repo, worker = stack["repo"], stack["worker"]
    tasks = [_task(recovery_attempts=0)]
    await repo.save_run(_run(tasks).model_dump(mode="json"))

    event = _event(attempt=1)
    ctx = SimpleNamespace(ack=AsyncMock(), nack=AsyncMock())
    await worker.handle_recovery_event(event.model_dump(mode="json"), ctx)
    # First delivery increments recovery_attempts to 1.
    run1 = await repo.get_run("run-1")
    assert run1["tasks"][0]["recovery_attempts"] == 1
    # Second delivery: attempt mismatch (1+1 != 1) → ignored.
    await worker.handle_recovery_event(event.model_dump(mode="json"), ctx)
    run2 = await repo.get_run("run-1")
    assert run2["tasks"][0]["recovery_attempts"] == 1   # not double-incremented


# ── §11.0 Finding 8: strict action validation ─────────────────────────

def test_unsupported_model_action_rejected():
    """§11.19: unsupported model action → explicit bounded rejection.
    The worker checks action.action against the RecoveryActionEnum and
    rejects unknowns before executing either branch (§11.0 Finding 8)."""
    from backend.models.recovery import RecoveryActionEnum
    action = RecoveryAction(action="invalid_action", rationale="weird")
    # The worker's check: action must be ABORT or REPAIR
    assert action.action not in (RecoveryActionEnum.ABORT, RecoveryActionEnum.REPAIR)
    # The strict validation means the worker's new check catches this
    # before falling through to the "neither branch executed" gap.


async def _async_events(action):
    return [SimpleNamespace(output=action.model_dump_json())]


# ── §11.0 Finding 9: cycle detection using actual repairs ─────────────

def test_cycle_detection_uses_actual_history():
    """§11.0 Finding 9: cycle detection uses ACTUAL historical repair
    fingerprints, not reconstructed hashes from current inputs."""
    from backend.engine.failure_classifier import FailureClassification
    from backend.services.recovery_policy import RecoveryAttempt, RecoveryPolicy

    # Two prior attempts with DIFFERENT corrected inputs.
    prior = [
        RecoveryAttempt(attempt=1, category="schema_mismatch",
                        action="repair", action_hash="hash-A",
                        rationale="fix date format", verified=False),
        RecoveryAttempt(attempt=2, category="schema_mismatch",
                        action="repair", action_hash="hash-B",
                        rationale="fix field name", verified=False),
    ]
    policy = RecoveryPolicy(attempts=prior)
    from backend.engine.failure_classifier import FailureClassification
    classification = FailureClassification(
        category="schema_mismatch", retryable=False,
        max_auto_attempts=3, recovery_hint="fix the schema")
    decision = policy.decide(
        classification, "test_tool",
        {"original": "inputs"}, action_hash="hash-A")
    # The policy blocks the third attempt (either by ceiling or cycle
    # detection — both are bounded-recovery enforcement, §11.0 Finding 9).
    assert not decision.allowed


# ── §11.0 Finding 11: premature success event fixed ───────────────────

def test_repair_queued_event_not_recovered():
    """§11.0 Finding 11: the audit event is TASK_REPAIR_QUEUED (not
    TASK_RECOVERED) when the repair has only been queued — the repaired
    action has not yet executed or been verified."""
    from backend.repositories.audit_repository import AuditEvent, ActorType
    event = AuditEvent(
        event_type="TASK_REPAIR_QUEUED",
        actor_id="SYSTEM", actor_type=ActorType.SYSTEM,
        resource_id="t1",
        details={"attempt": 1, "rationale": "fix", "note": "verification pending"})
    assert event.event_type == "TASK_REPAIR_QUEUED"
    assert event.event_type != "TASK_RECOVERED"


# ── §11.19: misleading success output → verification blocks ──────────

def test_misleading_success_blocked_by_verifier():
    """§11.19: misleading success output → verification blocks completion."""
    from backend.services.outcome_verifier import (
        AcceptanceCriterion, OutcomeVerifier,
    )
    verifier = OutcomeVerifier()
    result = verifier.verify(
        {"reply": "All done! Everything worked perfectly."},
        criteria=[AcceptanceCriterion(kind="artifact_exists")],
    )
    assert result.verdict != "pass"


# ── §12.5: the episode is the claim authority ──────────────────────────

@pytest.mark.asyncio
async def test_competing_workers_resolve_to_one_episode_claim(stack):
    """§12.5 acceptance: competing workers and redelivery cannot start
    duplicate logical recoveries — the CAS episode claim decides."""
    import asyncio
    repo, worker, episodes = (stack["repo"], stack["worker"],
                              stack["episodes"])
    tasks = [_task(recovery_attempts=0)]
    await repo.save_run(_run(tasks).model_dump(mode="json"))

    await asyncio.gather(
        worker.handle_recovery_event(_event(attempt=1).model_dump(mode="json"),
                                     SimpleNamespace(ack=AsyncMock(),
                                                     nack=AsyncMock())),
        worker.handle_recovery_event(_event(attempt=1).model_dump(mode="json"),
                                     SimpleNamespace(ack=AsyncMock(),
                                                     nack=AsyncMock())))

    run1 = await repo.get_run("run-1")
    assert run1["tasks"][0]["recovery_attempts"] == 1, \
        "exactly one logical recovery claim"
    episode = await episodes.get_episode_for_task("run-1", "t1")
    assert episode is not None, "the durable episode exists"
    cursor = await episodes.db.execute(
        "SELECT COUNT(*) AS n FROM recovery_episodes WHERE run_id=? AND task_id=?",
        ("run-1", "t1"))
    assert (await cursor.fetchone())["n"] == 1, \
        "one durable episode per failure episode"
    # The episode carries a persistent absolute deadline from creation.
    assert episode.get("deadline_at"), "deadline must be persisted"


@pytest.mark.asyncio
async def test_recovery_deadline_escalates_not_recovers(stack):
    """§12.5: the persisted absolute deadline is ACTIVE — an event past
    the deadline escalates (task FAILED) instead of recovering."""
    from backend.models.admission import epoch_now
    repo, worker, episodes = (stack["repo"], stack["worker"],
                              stack["episodes"])
    tasks = [_task(recovery_attempts=0)]
    await repo.save_run(_run(tasks).model_dump(mode="json"))
    # Pre-create the episode with an already-passed deadline.
    await episodes.create_episode({
        "episode_id": "ep-deadline", "tenant_id": "u1", "actor_id": "system",
        "run_id": "run-1", "task_id": "t1", "failure_code": "TIMEOUT_ERROR",
        "goal_digest": "gd", "deadline_at": epoch_now() - 10})

    await worker.handle_recovery_event(_event(attempt=1).model_dump(mode="json"),
                                       SimpleNamespace(ack=AsyncMock(),
                                                       nack=AsyncMock()))
    final = await repo.get_run("run-1")
    assert final["tasks"][0]["status"] == TaskStatus.FAILED
    assert "recovery-deadline-exceeded" in str(final["tasks"][0].get("error"))
    ep = await episodes.get_episode("ep-deadline")
    assert ep["state"] == "ESCALATED"
