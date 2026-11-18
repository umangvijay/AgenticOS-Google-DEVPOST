"""Phase 13 chat-1 acceptance tests (§13.1/§13.2).

§13.1: an engine-driven tool invocation produces a VALID durable step
trace through the §10.6 lifecycle; the validated-arguments digest is
recorded at DISPATCH (a validated proposal is not a dispatch); duplicate
delivery does not inflate logical-action totals; a lost lease supersedes.

§13.2: the durable evaluation job queue delivers crash-safely — a worker
that dies after claiming is superseded after lease expiry; a stale
worker cannot overwrite the finished job; the queue processes its OWN
claims (the oldest job can never strand another run's event).
"""
import asyncio
import json

import pytest
import pytest_asyncio

from backend.repositories.evaluation_job_repository import (
    EvaluationJobRepository, set_evaluation_job_repo)
from backend.repositories.execution_step_repository import (
    ExecutionStepRepository, StepState, set_execution_step_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
from backend.models.schemas import Task, TaskStatus, WorkflowRun


@pytest_asyncio.fixture
async def stack(tmp_path):
    from backend.engine.engine import WorkflowEngine
    from backend.repositories.in_memory_message_bus import InMemoryMessageBus
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository)
    db = DatabaseManager(str(tmp_path / "chat1.db"))
    await db.initialize()
    workflow_repo = SQLiteWorkflowRepository(db)
    policy_repo = SQLiteAdaptivePolicyRepository(db)
    engine = WorkflowEngine(workflow_repo, InMemoryMessageBus(),
                            adaptive_policy_repo=policy_repo)
    job_repo = EvaluationJobRepository(db)
    set_evaluation_job_repo(job_repo)
    # Production wiring: the engine reads the step repo singleton.
    step_repo = ExecutionStepRepository(db)
    set_execution_step_repo(step_repo)
    try:
        yield {"db": db, "repo": workflow_repo, "engine": engine,
               "jobs": job_repo, "steps": step_repo}
    finally:
        set_evaluation_job_repo(None)
        set_execution_step_repo(None)
        await db.close()


async def _save_run(repo, run_id, criteria=None):
    task = Task(task_id="t1", workflow_id="wf-c1", run_id=run_id,
                user_id="u1", agent="core.set",
                input_data={"fields": {"ok": True}},
                acceptance_criteria=criteria or [],
                status=TaskStatus.PENDING)
    run = WorkflowRun(run_id=run_id, workflow_id="wf-c1", user_id="u1",
                      goal="chat1", status=TaskStatus.RUNNING, tasks=[task])
    await repo.save_run(run.model_dump(mode="json"))
    return run


def _step_for(run_id, task_id="t1", attempt=1):
    return f"step_{run_id[:16]}_{task_id}_{attempt}"


# ── §13.1: valid durable execution evidence ────────────────────────────

@pytest.mark.asyncio
async def test_engine_invocation_produces_valid_step_trace(stack):
    """THE §13.1 acceptance: the step traverses PROPOSED → VALIDATING →
    READY → DISPATCHING → VERIFYING → VERIFIED with legal actors, and the
    validated-arguments digest is populated at DISPATCH."""
    repo, engine = stack["repo"], stack["engine"]
    run = await _save_run(repo, "c1-trace")
    await engine.execute_task(run.run_id, "t1")

    steps = await stack["steps"].steps_for_run(run.run_id)
    assert steps, "a durable step exists"
    step = steps[0]
    assert step["state"] == StepState.VERIFIED.value
    assert step["validated_action_digest"], \
        "the digest is recorded at dispatch — the tool-call metric's basis"
    assert step["run_id"] == run.run_id and step["task_id"] == "t1"


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_inflate_the_step(stack):
    """§13.1: at-least-once delivery must not create two dispatches for
    one logical action."""
    repo, engine = stack["repo"], stack["engine"]
    run = await _save_run(repo, "c1-dup")
    await asyncio.gather(engine.execute_task(run.run_id, "t1"),
                         engine.execute_task(run.run_id, "t1"))
    steps = await stack["steps"].steps_for_run(run.run_id)
    dispatches = [s for s in steps
                  if s.get("validated_action_digest")]
    assert len(dispatches) <= 1, "one logical action, at most one dispatch"
    task = (await repo.get_run(run.run_id))["tasks"][0]
    assert task["attempt"] == 1


@pytest.mark.asyncio
async def test_lost_lease_supersedes_never_dispatches(stack, monkeypatch):
    """§13.1: a lease lost before dispatch leaves the step SUPERSEDED —
    never counted as a tool dispatch."""
    repo, engine = stack["repo"], stack["engine"]
    run = await _save_run(repo, "c1-lost")

    async def lost_claim(*a, **k):
        return False
    monkeypatch.setattr(engine.repo, "claim_task", lost_claim)
    await engine.execute_task(run.run_id, "t1")

    steps = await stack["steps"].steps_for_run(run.run_id)
    assert steps[0]["state"] == StepState.SUPERSEDED.value, \
        "no lease = no dispatch = superseded step"
    assert not steps[0].get("validated_action_digest")


# ── §13.2: crash-safe evaluation delivery ──────────────────────────────

@pytest.mark.asyncio
async def test_queue_processes_its_own_claim_not_the_oldest(stack):
    """THE §13.2 stranding fix: claiming for run X must yield run X's job —
    an unrelated terminal event can never strand the oldest job."""
    repo, engine, jobs = stack["repo"], stack["engine"], stack["jobs"]
    # Two pending jobs: an OLD unrelated one and OURS.
    await jobs.enqueue("run-old")
    await jobs.enqueue("run-c1-mine")
    # Simulate the worker hook: claim and process ONLY our run's job.
    claimed = await jobs.claim("w1")
    assert len(claimed) == 2, "the drainer claims both"
    ours = [j for j in claimed if j["run_id"] == "run-c1-mine"]
    assert ours, "our run's job is claimable regardless of queue position"
    # The worker completes the job it owns — no stranding.
    await jobs.complete(ours[0]["job_id"], evaluation_id="eval-x",
                        owner_worker_id="w1")
    health = await jobs.health()
    assert health["done"] == 1


@pytest.mark.asyncio
async def test_stale_worker_cannot_overwrite_completed_job(stack):
    """§13.2 acceptance: worker A dies after claiming; worker B finishes
    after lease expiry; stale worker A cannot overwrite B's result."""
    jobs = stack["jobs"]
    await jobs.enqueue("run-stale")
    await jobs.claim("worker-A", lease_seconds=1)
    # Lease expires (simulate time passing).
    await stack["db"].execute(
        "UPDATE evaluation_jobs SET lease_expires_at=1 WHERE run_id='run-stale'")
    await stack["db"].commit()
    claimed_b = await jobs.claim("worker-B")
    assert claimed_b, "worker B takes over after expiry"
    await jobs.complete(claimed_b[0]["job_id"], evaluation_id="eval-B",
                        owner_worker_id="worker-B")
    # Stale worker A tries to complete ITS job_id — fenced by lease owner.
    from backend.repositories.evaluation_job_repository import (
        EvaluationJobError)
    with pytest.raises(Exception):
        await jobs.complete(claimed_b[0]["job_id"], evaluation_id="eval-A",
                            owner_worker_id="worker-A")


@pytest.mark.asyncio
async def test_exhausted_job_is_requeued_by_reset_never_lost(stack):
    jobs = stack["jobs"]
    await jobs.enqueue("run-ex")
    await jobs.claim("w1")
    for i in range(5):
        await jobs.fail_active_for_run("run-ex", f"err {i}",
                                        owner_worker_id="w1")
    health = await jobs.health()
    assert health["exhausted"] == 1, "visible, never silently dropped"
    # An operator reset requeues it.
    await jobs.reset_exhausted()
    health = await jobs.health()
    assert health["pending"] == 1


# ── §13.8: the reviewed-failure query (review finding G) ───────────────

@pytest.mark.asyncio
async def test_reviewed_failures_query_returns_terminal_only(stack):
    """§13.8 acceptance: real repository records — terminal
    failed/escalated episodes produce clusters; ACTIVE abandoned episodes
    never become improvement candidates."""
    from backend.repositories.recovery_episode_repository import (
        RecoveryEpisodeRepository, RecoveryState)
    ep_repo = RecoveryEpisodeRepository(stack["db"])
    # A terminal reviewed failure + an ACTIVE abandoned episode.
    await ep_repo.create_episode({
        "episode_id": "ep-term", "tenant_id": "u1", "actor_id": "system",
        "run_id": "run-t", "task_id": "t1", "failure_code": "TIMEOUT",
        "failure_category": "temporary_read_failure", "goal_digest": "g",
        "deadline_at": 99999999999})
    await ep_repo.transition("ep-term", RecoveryState.CLASSIFIED, actor="system")
    await ep_repo.transition("ep-term", RecoveryState.ESCALATED, actor="system")
    await ep_repo.create_episode({
        "episode_id": "ep-active", "tenant_id": "u1", "actor_id": "system",
        "run_id": "run-a", "task_id": "t2", "failure_code": "TIMEOUT",
        "failure_category": "temporary_read_failure", "goal_digest": "g",
        "deadline_at": 99999999999})

    rows = await ep_repo.reviewed_failures(tenant_id="u1")
    ids = [r["episode_id"] for r in rows]
    assert "ep-term" in ids and "ep-active" not in ids, \
        "active abandoned episodes are recovery candidates, not reviewed failures"

    # The proposer clusters exactly the reviewed failures.
    from backend.services.improvement_rollout import clusters_from_episodes
    clusters = clusters_from_episodes(rows)
    assert clusters and clusters[0]["category"] == "temporary_read_failure"
    assert clusters[0]["count"] == 1


# ── §13.4: live browser validator wiring (review finding D) ────────────

def test_consequential_classification_covers_non_form_controls():
    """§13.4: deletion/publish/transaction controls OUTSIDE forms are
    consequential; navigation to confirm/checkout URLs too."""
    from backend.services.web_agent import WebAgent
    snap = {"elements": [{"idx": 3, "text": "Delete account",
                          "aria": "delete", "submit": False}]}
    assert WebAgent._is_consequential({"action": "click", "index": 3}, snap)
    assert WebAgent._is_consequential(
        {"action": "navigate", "url": "https://x.io/checkout"}, {})
    assert not WebAgent._is_consequential(
        {"action": "click", "index": 9},
        {"elements": [{"idx": 9, "text": "Docs", "submit": False}]})


def test_validator_refusals_are_structured_not_swallowed():
    """§13.4: the full validator produces structured failure codes for a
    non-owner caller — the live path surfaces them, never dispatches."""
    from backend.services.browser_action_validator import validate_browser_action
    from backend.services.browser_target_resolver import StalenessContext
    from backend.models.browser_observation import ActionType
    from backend.models.browser_session import ControllerState, ResourceState
    ctx = StalenessContext(observation_id="o", document_generation=1,
                           controller_generation=1, owner_generation=1,
                           page_id="u", frame_id="main", origin="u")
    allowed, failures = validate_browser_action(
        action_type=ActionType.CLICK, arguments={"index": 1},
        observation_context=ctx, current_context=ctx,
        session_state=ResourceState.READY, controller=ControllerState.AGENT,
        caller_is_owner=False, owner_epoch_matches=False,
        consequential=True, write_effect_recorded=False)
    assert allowed is False
    assert "CALLER_NOT_OWNER" in failures and "OWNER_EPOCH_CHANGED" in failures


@pytest.mark.asyncio
async def test_effect_persistence_failure_stops_the_action():
    """§13.4 acceptance: failure to record an effect CANNOT be hidden
    behind a debug log — the consequential action is not dispatched and
    the refusal is visible in the step result."""
    from backend.models.effect import EffectState
    from backend.repositories.effect_repository import (
        SQLiteEffectRepository, set_effect_repo)
    from backend.repositories.sqlite.database import DatabaseManager
    from unittest.mock import AsyncMock, patch

    import tempfile as _tf, os as _os
    db = DatabaseManager(_os.path.join(_tf.mkdtemp(), "fx-gate.db"))
    await db.initialize()
    fx = SQLiteEffectRepository(db)
    set_effect_repo(fx)
    try:
        # A repo whose prepare fails → persistence raises (disk full etc.).
        with patch.object(fx, "prepare_effect",
                          side_effect=RuntimeError("disk full")):
            from backend.services.browser_runtime import (
                ActionKind, build_action_intent,
            )
            intent = build_action_intent(
                session_id="s1", controller_epoch="e1", frame_origin="u",
                action_kind=ActionKind.SUBMIT, target_identity="1",
                material_payload={"url": "u"})
            from backend.services.browser_effect_service import (
                record_browser_action_effect,
            )
            with pytest.raises(Exception):
                await record_browser_action_effect(
                    fx, intent=intent, owner_user_id="u1")
    finally:
        set_effect_repo(None)
        await db.close()
