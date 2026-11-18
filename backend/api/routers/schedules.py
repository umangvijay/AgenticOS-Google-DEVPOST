"""
AgentOS — Schedules Router

POST   /api/v1/schedules              — Create a scheduled workflow
GET    /api/v1/schedules              — List user's schedules
GET    /api/v1/schedules/{id}         — Get schedule detail
PUT    /api/v1/schedules/{id}         — Update schedule
DELETE /api/v1/schedules/{id}         — Delete schedule
POST   /api/v1/schedules/{id}/run     — Run a schedule immediately
POST   /api/v1/schedules/{id}/pause   — Pause/resume a schedule
GET    /api/v1/schedules/{id}/history — Get execution history
"""

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends, status, Query
from pydantic import BaseModel

from backend.api.dependencies.auth import get_current_user, AuthenticatedUser, require_not_viewer
from backend.security.input_sanitizer import sanitize_goal, sanitize_text, InputValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedules", tags=["schedules"])


def _get_factory(request: Request):
    factory = getattr(request.app.state, "factory", None)
    if not factory:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return factory


def _schedule_public(schedule: dict) -> dict:
    """Expose a stable is_enabled flag alongside the status string so the
    UI (which reads is_enabled) and API clients (which read status) agree."""
    s = dict(schedule or {})
    s["is_enabled"] = str(s.get("status") or "").upper() == "ACTIVE"
    return s


class CreateScheduleRequest(BaseModel):
    name: str
    goal: str
    cron_expression: str               # Standard 5-field cron
    timezone: str = "UTC"
    # §29.19 revision: spring-gap/fall-fold/missed-run behavior, frozen
    # at admission. coalesce_one (default) collapses missed windows into
    # ONE run; skip/first/second also pin the DST wall-clock policy.
    missed_run_policy: str = "coalesce_one"
    is_enabled: bool = True

class UpdateScheduleRequest(BaseModel):
    name: Optional[str] = None
    goal: Optional[str] = None
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_schedule(
    body: CreateScheduleRequest, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Create a new scheduled workflow."""
    factory = _get_factory(request)

    try:
        goal = sanitize_goal(body.goal)
        name = sanitize_text(body.name, "name", max_length=200)
    except InputValidationError as e:
        raise HTTPException(status_code=400, detail=e.message)

    # Validate cron expression
    try:
        from croniter import croniter
        if not croniter.is_valid(body.cron_expression):
            raise ValueError("Invalid")
    except (ValueError, ImportError):
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {body.cron_expression}")

    # §29.19: the timezone must be a real zone — an invalid one fails the
    # request instead of silently scheduling in UTC.
    from backend.services.schedule_time import (
        validate_timezone, resolve_occurrence, MISSED_RUN_POLICIES)
    try:
        resolved_tz = validate_timezone(body.timezone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if body.missed_run_policy not in MISSED_RUN_POLICIES:
        raise HTTPException(
            status_code=400,
            detail=f"missed_run_policy must be one of {MISSED_RUN_POLICIES}")

    schedule_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # §29.19 revision: the next occurrence is resolved into its canonical
    # UTC instant AND its logical LOCAL wall-clock occurrence — the pair
    # persisted together so DST gaps/folds are visible and the occurrence
    # identity survives restarts.
    resolved = resolve_occurrence(
        body.cron_expression, resolved_tz,
        gap_policy=("skip" if body.missed_run_policy == "coalesce_one"
                    else body.missed_run_policy),
        fold_policy=body.missed_run_policy
        if body.missed_run_policy in ("first", "second") else "first")
    next_run = resolved["utc_instant"] if resolved else None
    next_run_local = resolved["local_occurrence"] if resolved else None

    schedule = {
        "schedule_id": schedule_id,
        "user_id": user.user_id,
        "name": name,
        "goal": goal,
        "cron_expression": body.cron_expression,
        "schedule_type": "cron",
        "timezone": body.timezone,
        "missed_run_policy": body.missed_run_policy,
        "status": "ACTIVE" if body.is_enabled else "PAUSED",
        "next_run_at": next_run,
        "next_run_local": next_run_local,
        "created_at": now,
        "updated_at": now,
        "is_enabled": bool(body.is_enabled),
    }

    await factory.schedule_repo.create_schedule(schedule)

    await factory.audit_repo.log_event({
        "event_type": "SCHEDULE_CREATED",
        "actor_id": user.user_id, "actor_type": "USER",
        "resource_id": schedule_id,
        "details": {"name": name, "cron": body.cron_expression},
    })

    return schedule


@router.get("")
async def list_schedules(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List the user's schedules."""
    factory = _get_factory(request)
    schedules = await factory.schedule_repo.list_schedules(user.user_id)
    out = [_schedule_public(s) for s in schedules]
    return {"schedules": out, "count": len(out)}


@router.get("/{schedule_id}")
async def get_schedule(
    schedule_id: str, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Get schedule detail."""
    factory = _get_factory(request)
    schedule = await factory.schedule_repo.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if schedule.get("user_id") != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")
    return _schedule_public(schedule)


@router.put("/{schedule_id}")
async def update_schedule(
    schedule_id: str, body: UpdateScheduleRequest, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Update a schedule."""
    factory = _get_factory(request)
    schedule = await factory.schedule_repo.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if schedule.get("user_id") != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")

    updates = {}
    if body.name is not None:
        try:
            updates["name"] = sanitize_text(body.name, "name", max_length=200)
        except InputValidationError as e:
            raise HTTPException(status_code=400, detail=e.message)
    if body.goal is not None:
        try:
            updates["goal"] = sanitize_goal(body.goal)
        except InputValidationError as e:
            raise HTTPException(status_code=400, detail=e.message)
    if body.cron_expression is not None:
        try:
            from croniter import croniter
            if not croniter.is_valid(body.cron_expression):
                raise ValueError()
            updates["cron_expression"] = body.cron_expression
        except (ValueError, ImportError):
            raise HTTPException(status_code=400, detail="Invalid cron expression")
    if body.timezone is not None:
        # §29.19: an unknown timezone fails the update instead of
        # silently scheduling in UTC; the next run recomputes in the
        # (possibly new) zone after both fields are resolved.
        from backend.services.schedule_time import validate_timezone
        try:
            updates["timezone"] = validate_timezone(body.timezone)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if "cron_expression" in updates or "timezone" in updates:
        # §29.19: recompute the next run in the schedule's (possibly
        # updated) timezone — wall-clock semantics, DST-correct.
        from backend.services.schedule_time import next_run_at
        effective_cron = updates.get(
            "cron_expression", schedule.get("cron_expression") or "")
        effective_tz = updates.get("timezone", schedule.get("timezone")) or "UTC"
        updates["next_run_at"] = next_run_at(effective_cron, effective_tz)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    await factory.schedule_repo.update_schedule(schedule_id, updates)

    return await factory.schedule_repo.get_schedule(schedule_id)


@router.delete("/{schedule_id}")
async def delete_schedule(
    schedule_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Delete a schedule."""
    factory = _get_factory(request)
    schedule = await factory.schedule_repo.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if schedule.get("user_id") != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")

    await factory.schedule_repo.delete_schedule(schedule_id)
    return {"schedule_id": schedule_id, "deleted": True}


@router.post("/{schedule_id}/run")
async def run_now(
    schedule_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Execute a schedule immediately (on-demand run).

    The run is created synchronously and idempotently so it is observable
    in the workflows list the moment this returns; the worker picks the
    trigger up from the bus and executes it (create_if_absent keeps this
    exactly-once across API and worker)."""
    factory = _get_factory(request)
    schedule = await factory.schedule_repo.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if schedule.get("user_id") != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")

    # Phase 3 §3.10: run-now starts a real workflow run — admission-policy
    # group "workflow" (shared across replicas) guards trigger spam.
    from backend.security.rate_limiter import check_rate_limit, check_shared_rate_limit
    check_rate_limit(f"user:{user.user_id}", "workflow")
    await check_shared_rate_limit(f"user:{user.user_id}", group="workflow")

    from backend.models.schedule import SchedulerTriggerEvent
    from backend.scheduler.run_factory import build_scheduled_run, scheduled_run_id

    scheduled_time = datetime.now(timezone.utc)
    event = SchedulerTriggerEvent(schedule_id=schedule_id, scheduled_time=scheduled_time)
    run_id = scheduled_run_id(schedule_id, scheduled_time)

    # Materialize the run before returning so clients can immediately GET it.
    from backend.engine.repo_adapter import maybe_await
    run = build_scheduled_run(schedule, run_id)
    await maybe_await(factory.workflow_repo.create_if_absent(run.model_dump(mode="json")))

    await factory.message_bus.publish("agentos-scheduler-triggers", event.model_dump(mode="json"))
    await factory.schedule_repo.record_execution(schedule_id, run_id, "TRIGGERED")

    return {
        "schedule_id": schedule_id,
        "run_id": run_id,
        "status": "PENDING",
        "message": "Schedule execution triggered.",
        "goal": schedule.get("goal"),
    }


@router.post("/{schedule_id}/pause")
async def toggle_pause(
    schedule_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Toggle pause/resume for a schedule."""
    factory = _get_factory(request)
    schedule = await factory.schedule_repo.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if schedule.get("user_id") != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")

    currently_active = str(schedule.get("status", "ACTIVE")).upper() == "ACTIVE"
    new_status = "PAUSED" if currently_active else "ACTIVE"
    await factory.schedule_repo.update_schedule(schedule_id, {"status": new_status})

    return {"schedule_id": schedule_id, "status": new_status, "is_enabled": new_status == "ACTIVE"}


@router.get("/{schedule_id}/history")
async def get_history(
    schedule_id: str, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    limit: int = Query(20, ge=1, le=100),
):
    """Get execution history for a schedule."""
    factory = _get_factory(request)
    schedule = await factory.schedule_repo.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if schedule.get("user_id") != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")

    history = await factory.schedule_repo.get_execution_history(schedule_id, limit)
    return {"executions": history, "count": len(history)}


@router.post("/{schedule_id}/webhook-secret")
async def rotate_webhook_secret(schedule_id: str, request: Request,
                                user: AuthenticatedUser = Depends(require_not_viewer)):
    """Return a new signing secret once; store only encrypted secret material."""
    import secrets
    from backend.security.secrets_vault import secrets_vault
    factory = _get_factory(request)
    schedule = await factory.schedule_repo.get_schedule(schedule_id)
    if not schedule or (schedule.get("user_id") != user.user_id and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Schedule not found")
    secret = secrets.token_urlsafe(32)
    await factory.secrets_repo.store_secret(schedule["user_id"], f"webhook:{schedule_id}", secrets_vault.encrypt(secret))
    return {"secret": secret, "algorithm": "HMAC-SHA256", "signed_payload": "timestamp.raw_request_body"}
