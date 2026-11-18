"""Phase 29 — executable workflow plans and schedule time (§29.19).

The accepted plan is versioned and bound to the run durably; the engine
refuses to dispatch a loaded plan that no longer matches the accepted
version; schedule next-run computation is wall-clock in the schedule's
declared timezone (DST-correct) and an unknown timezone is rejected at
admission.
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest

from backend.engine.plan_repair import plan_version_hash


# ── plan versioning ──────────────────────────────────────────────────

class _T:
    def __init__(self, task_id, agent="core.chat", tool=None,
                 input_data=None, dependencies=(), timeout_seconds=60,
                 max_retries=3):
        self.task_id = task_id
        self.agent = agent
        self.tool = tool
        self.input_data = input_data or {}
        self.dependencies = list(dependencies)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries


def test_plan_version_hash_is_order_insensitive_and_content_sensitive():
    a = plan_version_hash([_T("t1"), _T("t2", dependencies=["t1"])])
    b = plan_version_hash([_T("t2", dependencies=["t1"]), _T("t1")])
    assert a == b, "task ORDER is not part of the DAG contract"
    c = plan_version_hash([_T("t1"), _T("t2", dependencies=["t1"],
                                      timeout_seconds=61)])
    assert c != a, "a changed timeout IS part of the executable plan"


def test_engine_refuses_to_dispatch_a_tampered_plan():
    """A run whose loaded tasks no longer match its accepted plan
    version must not dispatch anything."""
    import asyncio

    from backend.engine.engine import WorkflowEngine
    from backend.models.schemas import Task, WorkflowRun

    engine = WorkflowEngine.__new__(WorkflowEngine)

    async def run_case(accepted_version):
        def make_tasks():
            return [
                Task(task_id="t1", workflow_id="w", run_id="r1",
                     user_id="u", agent="core.chat"),
                Task(task_id="t2", workflow_id="w", run_id="r1",
                     user_id="u", agent="core.chat",
                     dependencies=["t1"]),
            ]

        tasks = make_tasks()
        run = WorkflowRun(run_id="r1", goal="g", status="RUNNING",
                          tasks=tasks)
        events = [{"type": "WORKFLOW_STARTED",
                   "sanitized_metadata": {"plan_version": accepted_version}}]
        published = []

        class _Bus:
            async def publish(self, topic, event):
                published.append(event)

        class _Repo:
            async def get_run(self, run_id):
                # The durable record: tasks are re-loaded fresh.
                return WorkflowRun(run_id="r1", goal="g", status="RUNNING",
                                   tasks=make_tasks()).model_dump(
                                       mode="json")

            async def get_events(self, run_id):
                return events

            async def update_task(self, *args, **kwargs):
                return None

            async def save_event(self, event):
                return None

        engine.repo = _Repo()
        engine.message_bus = _Bus()
        await engine.evaluate_dag("r1")
        return published

    accepted = plan_version_hash([
        _T("t1"), _T("t2", dependencies=["t1"])])
    published = asyncio.new_event_loop().run_until_complete(
        run_case(accepted))
    assert published, "an accepted plan dispatches its ready tasks"

    tampered = asyncio.new_event_loop().run_until_complete(
        run_case("0" * 64))
    assert tampered == [], "a tampered plan must not dispatch"

    # Legacy rows (no recorded version) keep executing.
    legacy = asyncio.new_event_loop().run_until_complete(run_case(None))
    assert legacy, "legacy rows dispatch without a version"


def test_start_events_carry_the_plan_version():
    """Both run-creation entry points bind the accepted plan version via
    the run's WORKFLOW_STARTED event metadata."""
    import inspect

    from backend.api import main as api_main
    from backend.api.routers import workflows as workflows_router

    for module in (api_main, workflows_router):
        src = inspect.getsource(module)
        assert "plan_version" in src, \
            f"{module.__name__} does not record a plan version"
        assert "plan_version_hash" in src, \
            f"{module.__name__} does not derive the version from tasks"


# ── schedule wall-clock time ─────────────────────────────────────────

def test_next_run_at_honors_the_declared_timezone():
    from backend.services.schedule_time import next_run_at
    # 09:00 in New York — 14:00 UTC during EDT.
    nxt = next_run_at("0 9 * * *", "America/New_York",
                      after=datetime(2026, 7, 15, 12, 0,
                                     tzinfo=dt_timezone.utc))
    parsed = datetime.fromisoformat(nxt)
    assert parsed.utcoffset() == dt_timezone.utc.utcoffset(parsed)
    in_ny = parsed.astimezone(ZoneInfo("America/New_York"))
    assert (in_ny.hour, in_ny.minute) == (9, 0), nxt


def test_next_run_at_is_dst_correct():
    from backend.services.schedule_time import next_run_at
    # Across the US DST boundary (Nov 1 2026, EDT→EST): 09:00 New York
    # is 13:00 UTC before the switch and 14:00 UTC after it.
    before = datetime.fromisoformat(next_run_at(
        "0 9 * * *", "America/New_York",
        after=datetime(2026, 10, 30, 12, 0, tzinfo=dt_timezone.utc)))
    after = datetime.fromisoformat(next_run_at(
        "0 9 * * *", "America/New_York",
        after=datetime(2026, 11, 2, 12, 0, tzinfo=dt_timezone.utc)))
    assert before.hour == 13, before
    assert after.hour == 14, after


def test_unknown_timezone_is_rejected_at_admission():
    from backend.services.schedule_time import validate_timezone
    with pytest.raises(ValueError):
        validate_timezone("Mars/Olympus_Mons")
    assert validate_timezone("UTC") == "UTC"
    assert validate_timezone("Europe/Berlin") == "Europe/Berlin"


def test_schedules_router_validates_timezone_and_recomputes_wall_clock():
    import inspect
    from backend.api.routers import schedules as schedules_router
    src = inspect.getsource(schedules_router)
    assert "validate_timezone" in src
    assert "next_run_at(" in src
