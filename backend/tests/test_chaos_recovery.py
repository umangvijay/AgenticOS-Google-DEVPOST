"""
AgentOS — Chaos & fault-injection tests (audit §9 "recovery chaos tests")

Fault injection against the REAL SQLite stack + workflow engine:
- duplicate message delivery (at-least-once bus) → exactly-once execution
- worker kill mid-run (lease never released) → lease expiry reclaim
- retry storm (persistent transient failure) → bounded retries, then FAILED
- adaptive policy learns from injected outcomes (UCB1 ordering)

These publish an honest answer to the audit's question "what happens when
the system is killed mid-run?" — verified, not asserted.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.config.settings import settings
from backend.engine.engine import WorkflowEngine
from backend.models.schemas import Task, TaskStatus, WorkflowRun
from backend.repositories.in_memory_message_bus import InMemoryMessageBus
from backend.repositories.sqlite.adaptive_policy_repository import SQLiteAdaptivePolicyRepository
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
from backend.services.adaptive_policy import AdaptivePolicy


@pytest_asyncio.fixture
async def stack(tmp_path):
    db = DatabaseManager(str(tmp_path / "chaos.db"))
    await db.initialize()
    workflow_repo = SQLiteWorkflowRepository(db)
    policy_repo = SQLiteAdaptivePolicyRepository(db)
    bus = InMemoryMessageBus()
    engine = WorkflowEngine(workflow_repo, bus, adaptive_policy_repo=policy_repo)
    yield {"db": db, "repo": workflow_repo, "engine": engine, "bus": bus, "policy": AdaptivePolicy(policy_repo)}
    await db.close()


async def _save_run(repo, run_id: str, agent: str = "core.set", **task_overrides) -> WorkflowRun:
    task = Task(
        task_id="t1", workflow_id="wf-chaos", run_id=run_id, user_id="u1",
        agent=agent, input_data={"fields": {"ok": True}},
        status=TaskStatus.PENDING, **task_overrides,
    )
    run = WorkflowRun(run_id=run_id, workflow_id="wf-chaos", user_id="u1",
                      goal="chaos drill", status=TaskStatus.RUNNING, tasks=[task])
    await repo.save_run(run.model_dump(mode="json"))
    return run


@pytest.mark.asyncio
async def test_duplicate_delivery_executes_exactly_once(stack):
    """At-least-once bus delivery must not double-execute a task."""
    repo, engine = stack["repo"], stack["engine"]
    run = await _save_run(repo, "chaos-dup-1")

    # The second execution must lose the claim race and leave one outcome.
    await asyncio.gather(engine.execute_task(run.run_id, "t1"),
                         engine.execute_task(run.run_id, "t1"))
    final = await repo.get_run(run.run_id)
    task = final["tasks"][0]
    assert task["status"] == TaskStatus.COMPLETED
    # Exactly-once: attempt count reflects a single successful claim.
    assert task["attempt"] == 1


@pytest.mark.asyncio
async def test_old_execution_cannot_overwrite_reclaimed_task(stack):
    from backend.models.exceptions import StaleTaskLeaseError
    repo = stack["repo"]
    run = await _save_run(repo, "fenced")
    assert await repo.claim_task(run.run_id, "t1", 60)
    await repo.update_task(run.run_id, "t1", {"lease_expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
    assert await repo.claim_task(run.run_id, "t1", 60)
    with pytest.raises(StaleTaskLeaseError):
        await repo.update_task(run.run_id, "t1", {"attempt": 1, "status": "COMPLETED", "output_data": {"stale": True}})
    current = await repo.get_task(run.run_id, "t1")
    assert current["attempt"] == 2 and current["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_worker_kill_midrun_is_reclaimed_after_lease_expiry(stack):
    """A worker that dies while holding a lease must not strand the task."""
    repo = stack["repo"]
    run = await _save_run(repo, "chaos-lease-1", timeout_seconds=60)

    # Worker claims the lease, then "dies" — the task stays RUNNING.
    claimed = await repo.claim_task(run.run_id, "t1", lease_seconds=60)
    assert claimed
    stranded = (await repo.get_run(run.run_id))["tasks"][0]
    assert stranded["status"] == TaskStatus.RUNNING
    assert stranded["attempt"] == 1

    # A second worker immediately after the crash CANNOT steal the live lease.
    assert await repo.claim_task(run.run_id, "t1", lease_seconds=60) is False

    # Forge lease expiry (the kill happened long ago). The next worker that
    # picks the task up IS the one that reclaims and executes it.
    past = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    await repo.update_task(run.run_id, "t1", {"lease_expires_at": past})
    await stack["engine"].execute_task(run.run_id, "t1")
    final = (await repo.get_run(run.run_id))["tasks"][0]
    assert final["status"] == TaskStatus.COMPLETED
    assert final["attempt"] == 2  # reclaimed, not restarted from scratch


@pytest.mark.asyncio
async def test_persistent_transient_failure_bounded_then_failed(stack):
    """Retry storm: transient failures retry with backoff, then fail bounded."""
    repo = stack["repo"]
    run = await _save_run(repo, "chaos-retry-1", agent="core.nonexistent", max_retries=1, timeout_seconds=30)

    # Every execution fails (unknown core node) and schedules bounded retries.
    for _ in range(6):
        await stack["engine"].execute_task(run.run_id, "t1")
        state = (await repo.get_run(run.run_id))["tasks"][0]
        if state["status"] in (TaskStatus.FAILED, TaskStatus.RETRYING):
            # clear any scheduled retry delay by claiming again via execute
            continue
    final = (await repo.get_run(run.run_id))["tasks"][0]
    assert final["status"] == TaskStatus.FAILED
    assert final["attempt"] <= 2 + 1  # bounded by max_retries=1 (+ recovery attempts)
    assert (final.get("output_data") or {}).get("failed") is True
    assert final.get("error_type")


@pytest.mark.asyncio
async def test_backoff_is_bounded_and_growing(stack):
    engine = stack["engine"]
    d1 = engine._calculate_backoff(1)
    d3 = engine._calculate_backoff(3)
    assert 2.0 <= d1 <= 2.0 * 1.1 + 0.2
    assert d3 > d1
    assert d3 <= 60.0 * 1.1 + 0.2  # capped at max_delay with jitter


@pytest_asyncio.fixture
async def no_epsilon(monkeypatch):
    import backend.services.adaptive_policy as ap
    monkeypatch.setattr(ap.random, "random", lambda: 0.99)


@pytest.mark.asyncio
async def test_adaptive_policy_learns_from_injected_outcomes(stack, no_epsilon):
    """The self-evolving loop: outcomes recorded → ranking changes."""
    policy = stack["policy"]
    candidates = ["gemini-3.8-flash", "gemini-4-argon"]

    # Before any evidence: stable order, both candidates present.
    assert await policy.rank(candidates) == candidates

    # flash succeeds fast; argon fails repeatedly → policy prefers flash.
    for _ in range(4):
        await policy.record("gemini-3.8-flash", True, 900.0)
        await policy.record("gemini-4-argon", False, 4000.0)
    ranked = await policy.rank(candidates)
    assert ranked[0] == "gemini-3.8-flash"

    # Leaderboard exposes the learning honestly.
    board = await policy.leaderboard()
    by_model = {m["model"]: m for m in board}
    assert by_model["gemini-3.8-flash"]["success_rate"] == 1.0
    assert by_model["gemini-4-argon"]["success_rate"] == 0.0
    assert by_model["gemini-3.8-flash"]["score"] > by_model["gemini-4-argon"]["score"]


@pytest.mark.asyncio
async def test_policy_ranking_never_drops_candidates(stack, no_epsilon):
    policy = stack["policy"]
    for _ in range(10):
        await policy.record("m-good", True, 100.0)
    ranked = await policy.rank(["m-good", "m-unknown", "m-bad"])
    assert sorted(ranked) == ["m-bad", "m-good", "m-unknown"]
    assert ranked[0] == "m-good"
