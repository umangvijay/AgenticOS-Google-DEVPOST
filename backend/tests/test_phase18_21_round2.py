"""Round-2 acceptance tests: durable evaluation jobs (§12.11), the
automation inbox (§21.6), run comparison (§21.3), and task-family
evaluators (§12.17)."""
import time

import pytest
import pytest_asyncio

from backend.repositories.evaluation_job_repository import (
    EvaluationJobRepository, set_evaluation_job_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.task_family_evaluators import (
    criteria_for_family, family_for_agent,
)
from backend.services.workflow_templates import compare_runs


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "round2.db"))
    await db.initialize()
    job_repo = EvaluationJobRepository(db)
    set_evaluation_job_repo(job_repo)
    try:
        yield {"db": db, "jobs": job_repo}
    finally:
        set_evaluation_job_repo(None)
        await db.close()


# ── §12.11: durable evaluation jobs ────────────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_terminal_events_return_the_same_job(env):
    jobs = env["jobs"]
    first = await jobs.enqueue("run-ej")
    second = await jobs.enqueue("run-ej")
    assert first["job_id"] == second["job_id"], "create-or-get identity"
    rows = await jobs.health()
    assert rows["by_state"].get("pending", 0) == 1


@pytest.mark.asyncio
async def test_claim_lease_and_complete(env):
    jobs = env["jobs"]
    job = await jobs.enqueue("run-ej2")
    claimed = await jobs.claim("w1")
    assert [j["job_id"] for j in claimed] == [job["job_id"]]
    # A second worker cannot claim the leased job.
    assert await jobs.claim("w2") == []
    await jobs.complete(job["job_id"], evaluation_id="eval-1",
                        owner_worker_id="w1")
    health = await jobs.health()
    assert health["done"] == 1
    # Re-enqueue returns the DONE job (not a new one).
    again = await jobs.enqueue("run-ej2")
    assert again["job_id"] == job["job_id"] and again["state"] == "done"


@pytest.mark.asyncio
async def test_failed_jobs_requeue_then_exhaust_visibly(env):
    jobs = env["jobs"]
    job = await jobs.enqueue("run-ej3")
    await jobs.claim("w1")
    for i in range(5):
        await jobs.fail(job["job_id"], f"attempt {i} failed",
                        owner_worker_id="w1")
    final = await jobs.enqueue("run-ej3")
    assert final["state"] == "exhausted", \
        "exhausted jobs park visibly — never silently dropped"
    assert final["attempts"] == 5
    health = await jobs.health()
    assert health["exhausted"] == 1


# ── §21.6: automation inbox ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inbox_aggregates_actionable_items(env):
    from backend.services.automation_inbox import AutomationInboxService
    from backend.repositories.sqlite.workflow_repository import (
        SQLiteWorkflowRepository)
    from backend.models.schemas import Task, TaskStatus, WorkflowRun
    repo = SQLiteWorkflowRepository(env["db"])
    # A failed run + a needs-review run for u1.
    for rid, status in (("run-inbox-f", TaskStatus.FAILED),
                        ("run-inbox-n", TaskStatus.NEEDS_REVIEW)):
        task = Task(task_id="t1", workflow_id="wf-i", run_id=rid, user_id="u1",
                    agent="core.set", status=TaskStatus.PENDING)
        run = WorkflowRun(run_id=rid, workflow_id="wf-i", user_id="u1",
                          goal="inbox drill", status=status, tasks=[task])
        await repo.save_run(run.model_dump(mode="json"))
    await repo.save_run(WorkflowRun(
        run_id="run-other", workflow_id="wf-i", user_id="someone-else",
        goal="private", status=TaskStatus.FAILED,
        tasks=[Task(task_id="t1", workflow_id="wf-i", run_id="run-other",
                    user_id="someone-else", agent="core.set",
                    status=TaskStatus.PENDING)]).model_dump(mode="json"))

    svc = AutomationInboxService(repo)
    out = await svc.collect("u1")
    kinds = sorted(i["kind"] for i in out["items"])
    assert kinds == ["failed", "needs_review"], \
        "the user's actionable items, nobody else's"
    assert out["counts"]["failed"] == 1
    # Every item carries the precise next action and link.
    for item in out["items"]:
        assert item["next_action"] and item["link"]


# ── §21.3: run comparison ──────────────────────────────────────────────

def test_run_comparison_reports_deltas_and_same_fixture():
    a = [{"record": {"success": True, "completed_tasks": 3, "failed_tasks": 0,
                     "recovery_attempts": 1, "tool_calls": 5,
                     "token_usage_total": 1200, "fixture_hash": "fh-1",
                     "evaluator_version": "3.0.0"}}]
    b = [{"record": {"success": True, "completed_tasks": 3, "failed_tasks": 0,
                     "recovery_attempts": 0, "tool_calls": 4,
                     "token_usage_total": 900, "fixture_hash": "fh-1",
                     "evaluator_version": "3.0.0"}}]
    out = compare_runs(a, b, label_a="baseline", label_b="candidate")
    assert out["deltas"]["token_usage_total"]["delta"] == -300
    assert out["same_fixture"] is True, "comparability flagged honestly"
    assert "never re-executed" in out["note"]


def test_comparison_without_evaluations_is_explicit():
    out = compare_runs([], [])
    assert out["a"]["evaluations"] == 0 and out["b"]["evaluations"] == 0


# ── §12.17: task-family evaluators ─────────────────────────────────────

def test_api_mutation_requires_provider_evidence_not_nonempty_reply():
    """THE §12.17 point: a mutation is never verified by a non-empty
    reply — provider evidence (effect_resolved) is mandatory."""
    criteria = criteria_for_family("api_mutation")
    kinds = [c["kind"] for c in criteria]
    assert "effect_resolved" in kinds
    assert "non_empty" not in kinds


def test_every_family_has_deterministic_checks():
    for family in ("api_read", "api_mutation", "browser_task",
                   "research_report", "resume", "connector_build",
                   "recovery", "code_project"):
        c1 = criteria_for_family(family, {"expected_content": "x"})
        c2 = criteria_for_family(family, {"expected_content": "x"})
        assert c1 == c2, f"{family} checks are deterministic"
        assert c1, f"{family} has mandatory checks"


def test_unknown_family_refused():
    import pytest
    with pytest.raises(ValueError, match="Unknown task family"):
        criteria_for_family("vibes")


def test_family_for_agent_maps_the_product_families():
    assert family_for_agent("core.email") == "api_mutation"
    assert family_for_agent("browser.navigate") == "browser_task"
    assert family_for_agent("core.search") == "api_read"
    assert family_for_agent("resume_builder") == "resume"
    assert family_for_agent("core.mcp_build") == "connector_build"
