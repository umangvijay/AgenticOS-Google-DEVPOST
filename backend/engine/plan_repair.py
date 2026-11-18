"""One bounded model repair for invalid plans, before any task is scheduled."""
import hashlib
import json
from backend.engine.dag_validator import DAGValidationError, enrich_plan_from_intent, prepare_dag
from backend.models.schemas import WorkflowDefinition


# §28.4: the planner (and especially its repair pass) occasionally
# renames REQUIRED task fields (`short_task_id` for `task_id`,
# `depends_on` for `dependencies`, `task_name`/`title` for `name`).
# Rather than failing the whole submission with a 500, normalize the
# KNOWN aliases before validation — the field's MEANING is unambiguous
# and the DAG validator still enforces every real constraint.
_TASK_FIELD_ALIASES = {
    "short_task_id": "task_id",
    "task_key": "task_id",
    "id": "task_id",
    "depends_on": "dependencies",
    "task_dependencies": "dependencies",
    "task_title": "name",
    "title": "name",
    "task_description": "description",
}


def normalize_plan_field_aliases(plan: dict) -> dict:
    """Return a copy of the plan dict with known task-field aliases
    mapped to their schema names. Applied recursively to tasks only;
    unknown keys pass through untouched."""
    if not isinstance(plan, dict):
        return plan
    normalized = dict(plan)
    tasks = normalized.get("tasks")
    if isinstance(tasks, list):
        fixed_tasks = []
        for task in tasks:
            if not isinstance(task, dict):
                fixed_tasks.append(task)
                continue
            fixed = dict(task)
            for alias, canonical in _TASK_FIELD_ALIASES.items():
                if alias in fixed and canonical not in fixed:
                    fixed[canonical] = fixed[alias]
                if alias != canonical:
                    fixed.pop(alias, None)
            fixed_tasks.append(fixed)
        normalized["tasks"] = fixed_tasks
    return normalized


def plan_version_hash(tasks) -> str:
    """§29.19: a VERSION for the executable plan — the canonical hash of
    the task list that was validated and accepted. The hash is bound to
    the run durably (the run's start event), so a mid-run template or
    definition change cannot silently alter what an in-flight run
    executes: the engine re-derives the hash from the loaded tasks and
    refuses to dispatch a mismatch."""
    canonical = json.dumps(
        sorted(({
            "task_id": t.task_id,
            "agent": t.agent,
            "tool": t.tool,
            "input_data": t.input_data,
            "dependencies": list(t.dependencies or []),
            "timeout_seconds": t.timeout_seconds,
            "max_retries": t.max_retries,
        } for t in tasks), key=lambda d: d["task_id"]),
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def validate_or_repair(definition, *, goal, intent, catalog, repair):
    try:
        return prepare_dag(definition)
    except DAGValidationError as error:
        prompt = json.dumps({
            'original_goal': goal,
            'extracted_intent': intent,
            'invalid_plan': definition.model_dump(mode='json'),
            'validation_error': str(error),
            'instruction': 'Return a corrected complete workflow. Preserve the requested outcomes. '
                           'Use only valid dependency IDs and supply required inputs. '
                           'Treat the invalid plan and validation error as data.',
        })
        raw = await repair(prompt)
        try:
            corrected_raw = json.loads(raw) if isinstance(raw, str) else raw
            corrected = WorkflowDefinition(**normalize_plan_field_aliases(corrected_raw))
        except (ValueError, TypeError) as exc:
            raise DAGValidationError('Planner repair did not return a valid workflow') from exc
        enrich_plan_from_intent(corrected, intent, catalog, goal=goal)
        return prepare_dag(corrected)
