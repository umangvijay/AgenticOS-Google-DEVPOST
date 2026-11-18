"""
AgentOS — Scheduled Run Factory

Shared, deterministic construction of the WorkflowRun a schedule fires.
Both the API (run-now) and the worker (cron trigger) build the same
run_id from the same execution key, so either side can create it first:
create_if_absent arbitrates and execution stays exactly-once.

The API creates the run synchronously on run-now so the run is
immediately observable in the workflows list — the audit's Round-2
finding ("scheduled runs are not observable right after triggering")
was async materialization, not an orphaned id.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from backend.models.schemas import Task, WorkflowRun

logger = logging.getLogger(__name__)


def scheduled_run_id(schedule_id: str, scheduled_time: datetime) -> str:
    """Deterministic run id: schedule_id + logical scheduled time."""
    execution_key = f"{schedule_id}|{scheduled_time.isoformat()}"
    return "sch-" + hashlib.sha256(execution_key.encode("utf-8")).hexdigest()


def build_scheduled_run(schedule: Dict[str, Any], run_id: str) -> WorkflowRun:
    """Construct the dynamic WorkflowRun containing the schedule's goal."""
    from backend.engine.direct_plan import plan_from_goal

    user_id = schedule["user_id"]
    goal = schedule["goal"]
    run = WorkflowRun(
        run_id=run_id,
        user_id=user_id,
        goal=goal,
        status="PENDING",
        tasks=[],
    )

    from backend.engine.task_titles import task_title

    direct = plan_from_goal(goal)
    if direct:
        for definition in direct.tasks:
            run.tasks.append(Task(
                **definition.model_dump(), workflow_id=run.workflow_id,
                run_id=run_id, user_id=user_id,
                title=task_title(definition),
            ))
    else:
        run.tasks.append(Task(
            task_id=f"root-{run_id}", workflow_id=run.workflow_id,
            run_id=run_id, user_id=user_id,
            agent="OrchestratorAgent", input_data={"goal": goal},
            status="PENDING", dependencies=[], timeout_seconds=300,
            title=f"Scheduled: {goal[:80]}",
        ))
    return run
