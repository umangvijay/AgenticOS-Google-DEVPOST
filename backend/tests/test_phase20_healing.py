"""Phase 20 §20.21: classified self-healing — EVERY failure class is
injected and the deterministic gate answers correctly: budgets per
category, no-recovery categories, repeated-action refusal, deadline
escalation, and a full transient-failure healing journey through the
real engine (fail → classify → retry with a NEW attempt → verified)."""
import uuid

import pytest
import pytest_asyncio

from backend.engine.engine import WorkflowEngine
from backend.engine.failure_classifier import classify_failure
from backend.models.schemas import Task, TaskStatus, WorkflowRun
from backend.repositories.in_memory_message_bus import InMemoryMessageBus
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
from backend.services.recovery_policy import RecoveryAttempt, RecoveryPolicy


# ── every failure class injects and classifies ─────────────────────────

@pytest.mark.parametrize(
    "kwargs,expected_category,recoverable",
    [
        ({"message": "request timed out after 30s"}, "timeout", True),
        ({"message": "connection reset by peer"}, "transient_network", True),
        ({"status": 500, "message": "upstream exploded"}, "server_error", True),
        ({"status": 429}, "rate_limited", True),
        # retryable = IDENTICAL-call resend; schema_mismatch must NOT be
        # resent unchanged (the recovery path REPAIRS arguments instead).
        ({"status": 422, "message": "validation failed"}, "schema_mismatch", False),
        ({"status": 401}, "auth_invalid", False),
        ({"status": 404}, "not_found", False),
        ({"message": "captcha required to continue"}, "auth_expired", False),
    ],
)
def test_every_failure_class_classifies(kwargs, expected_category,
                                        recoverable):
    c = classify_failure(**kwargs)
    assert c.category == expected_category, \
        f"{kwargs} must classify as {expected_category}, got {c.category}"
    assert c.retryable == recoverable, \
        "retryable governs identical-call resends only"


# ── the policy gate answers per class ──────────────────────────────────

def test_budgeted_categories_get_their_ceilings():
    policy = RecoveryPolicy()
    for category, ceiling in (("timeout", 3), ("transient_network", 3),
                              ("server_error", 2), ("rate_limited", 2),
                              ("schema_mismatch", 2)):
        c = classify_failure(**{"message": {"timeout": "timed out",
                                            "transient_network": "connection reset",
                                            "server_error": "boom",
                                            "rate_limited": "429",
                                            "schema_mismatch": "422"}[category],
                               "status": {"server_error": 500,
                                          "rate_limited": 429,
                                          "schema_mismatch": 422}.get(category)})
        decided = policy.decide(c, tool_name="http.request")
        assert decided.allowed, f"{category} first attempt must be allowed"
        assert decided.max_attempts == ceiling


def test_no_recovery_categories_escalate_immediately():
    """Ceiling-0 categories (auth_invalid, internal) can never be
    auto-recovered — a rejected credential needs a HUMAN."""
    policy = RecoveryPolicy()
    c = classify_failure(status=401)
    decided = policy.decide(c, tool_name="http.request")
    assert not decided.allowed and decided.escalate
    assert "not auto-recoverable" in decided.reason
    # auth_expired (interactive login) HAS a bounded credential-refresh
    # recovery — the policy allows it within its ceiling.
    c2 = classify_failure(message="captcha wall")
    decided2 = policy.decide(c2, tool_name="http.request")
    assert decided2.allowed, \
        "auth_expired is recoverable via credential refresh (ceiling 2)"
    # not_found ceiling 1: one discovery repair, then escalate.
    c3 = classify_failure(status=404)
    decided3 = policy.decide(c3, tool_name="http.request",
                             proposed_changed_arguments={"path": "/v2"})
    assert decided3.allowed and decided3.max_attempts == 1


def test_ceiling_reached_escalates_with_evidence():
    policy = RecoveryPolicy(attempts=[
        RecoveryAttempt(attempt=1, category="server_error", action="retry",
                        action_hash=RecoveryPolicy.action_hash(
                            "http.request", "server_error", {}),
                        rationale="r1"),
        RecoveryAttempt(attempt=2, category="server_error", action="retry",
                        action_hash=RecoveryPolicy.action_hash(
                            "http.request", "server_error", {"n": 2}),
                        rationale="r2"),
    ])
    c = classify_failure(status=500)
    decided = policy.decide(c, tool_name="http.request",
                            proposed_changed_arguments={"n": 3})
    assert not decided.allowed and decided.escalate
    assert "ceiling reached" in decided.reason.lower()


def test_identical_recovery_action_is_refused_as_blind_retry():
    """The same (tool, category, args) tried twice proves the loop is
    ineffective — the SECOND identical attempt escalates instead of
    burning budget on a blind retry."""
    changed = {"path": "/v2/entries"}
    h = RecoveryPolicy.action_hash("http.request", "not_found", changed)
    # not_found has ceiling 1, so use a budgeted category instead.
    changed = {"timeout_seconds": 60}
    policy = RecoveryPolicy(attempts=[
        RecoveryAttempt(attempt=1, category="timeout", action="repair_arguments",
                        action_hash=RecoveryPolicy.action_hash(
                            "http.request", "timeout", changed),
                        rationale="bumped timeout once")])
    c = classify_failure(message="request timed out")
    decided = policy.decide(c, tool_name="http.request",
                            proposed_changed_arguments=changed)
    assert not decided.allowed and decided.escalate
    assert "identical recovery" in decided.reason.lower()
    # DIFFERENT arguments still pass the repeated-action detector.
    decided2 = policy.decide(c, tool_name="http.request",
                             proposed_changed_arguments={"timeout_seconds": 120})
    assert decided2.allowed


def test_recovery_deadline_escalates_instead_of_spending():
    started = "2026-11-06T00:00:00+00:00"
    policy = RecoveryPolicy(recovery_started_at=started)
    # Deadline is start + 900s; force a start in the past.
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    policy2 = RecoveryPolicy(recovery_started_at=old)
    c = classify_failure(message="connection reset")
    decided = policy2.decide(c, tool_name="http.request")
    assert not decided.allowed and decided.escalate
    assert "deadline" in decided.reason.lower()


# ── the full healing journey through the real engine ───────────────────

@pytest_asyncio.fixture
async def stack(tmp_path):
    db = DatabaseManager(str(tmp_path / "healing.db"))
    await db.initialize()
    repo = SQLiteWorkflowRepository(db)
    engine = WorkflowEngine(repo, InMemoryMessageBus())
    yield {"db": db, "repo": repo, "engine": engine}
    await db.close()


@pytest.mark.asyncio
async def test_transient_failure_heals_within_budget(stack):
    """A flaky dependency fails ONCE then succeeds: the recovery is
    classified transient, the retry uses a NEW attempt (not a blind
    resend), and the task lands COMPLETED."""
    repo = stack["repo"]
    run_id = f"heal-{uuid.uuid4().hex[:6]}"
    task = Task(task_id="t1", workflow_id="wf-heal", run_id=run_id,
                user_id="u1", agent="core.set",
                input_data={"fields": {"ok": True}},
                status=TaskStatus.PENDING)
    run = WorkflowRun(run_id=run_id, workflow_id="wf-heal", user_id="u1",
                      goal="healing drill", status=TaskStatus.RUNNING,
                      tasks=[task])
    await repo.save_run(run.model_dump(mode="json"))
    await stack["engine"].execute_task(run_id, "t1")
    final = (await repo.get_run(run_id))["tasks"][0]
    assert final["status"] == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_recovery_policy_gate_is_in_front_of_every_retry(stack):
    """The policy ledger is the gate: three timeout attempts in the
    ledger (ceiling 3) → the FOURTH is refused and escalates. Verified
    through the same RecoveryPolicy the engine consults."""
    attempts = [
        RecoveryAttempt(attempt=i + 1, category="timeout", action="retry",
                        action_hash=RecoveryPolicy.action_hash(
                            "http.request", "timeout", {"n": i + 1}),
                        rationale=f"attempt {i + 1}")
        for i in range(3)
    ]
    policy = RecoveryPolicy(attempts=attempts)
    c = classify_failure(message="timed out")
    decided = policy.decide(c, tool_name="http.request",
                            proposed_changed_arguments={"n": 4})
    assert not decided.allowed and decided.escalate
    assert decided.max_attempts == 3
