"""Command lifecycle API (Phase 4 §4.19).

Stable command contracts over the durable lifecycle:
- GET  /commands/{id}          — current durable state (the receipt)
- POST /commands/{id}/cancel   — record cancellation intent (§4.15 protocol)
- POST /commands/{id}/retry    — authorized retry of a FAILED command
- GET  /commands/{id}/events   — authorized command event history

A transport timeout after submission means acceptance is UNKNOWN to the
client, never rejection: clients resolve via GET with the same idempotency
key. Existing workflow/build endpoints remain the primary surface; these are
the shared lifecycle contracts underneath them.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
from backend.services import command_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/commands", tags=["commands"])


@router.get("/{command_id}")
async def get_command(command_id: str, request: Request,
                      user: AuthenticatedUser = Depends(get_current_user)):
    """§4.19: the current durable state of a command. Unknown acceptance
    after a transport timeout resolves HERE, not by blind resubmission."""
    receipt = await command_service.get_receipt(command_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND", "message": "No such command",
            "accepted": False})
    repo = command_service.get_command_repo()
    command = await repo.get_command(command_id)
    if command.owner_user_id != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")
    return receipt


@router.get("/by-run/{run_id}")
async def get_command_by_run(run_id: str, request: Request,
                             user: AuthenticatedUser = Depends(get_current_user)):
    """C15/C16: the frontend knows a run's id from any submission — resolve
    its command receipt so unknown acceptance and lifecycle states are
    recoverable without having held the command id client-side."""
    repo = command_service.get_command_repo()
    if repo is None:
        raise HTTPException(status_code=503, detail="Command lifecycle unavailable")
    command = await repo.get_command_by_run(run_id)
    if command is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND",
                                                     "accepted": False})
    if command.owner_user_id != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")
    receipt = await command_service.get_receipt(command.command_id)
    return receipt


@router.post("/{command_id}/cancel")
async def cancel_command(command_id: str, request: Request,
                         user: AuthenticatedUser = Depends(get_current_user)):
    """§4.15: persist cancellation INTENT. The response distinguishes
    'requested' from 'stopped' from 'already finished' — never implying that
    cancellation reverses committed external effects."""
    repo = command_service.get_command_repo()
    if repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "ADMISSION_UNAVAILABLE",
            "message": "Command lifecycle is not configured on this deployment"})
    command = await repo.get_command(command_id)
    if command is None:
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND", "message": "No such command", "accepted": False})
    if command.owner_user_id != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")
    factory = getattr(request.app.state, "factory", None)
    result = await command_service.request_cancellation(
        command_id, by=user.user_id, reason="user request",
        workflow_repo=getattr(factory, "workflow_repo", None))
    return result


@router.post("/{command_id}/retry")
async def retry_command(command_id: str, request: Request,
                        user: AuthenticatedUser = Depends(get_current_user)):
    """§4.19: explicitly request an authorized retry of a FAILED command —
    a NEW idempotency key under the same logical intent, passing the same
    admission gates as any new command."""
    repo = command_service.get_command_repo()
    if repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "ADMISSION_UNAVAILABLE",
            "message": "Command lifecycle is not configured on this deployment"})
    command = await repo.get_command(command_id)
    if command is None:
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND", "message": "No such command", "accepted": False})
    if command.owner_user_id != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")
    if command.state.value != "FAILED":
        raise HTTPException(status_code=409, detail={
            "code": "INVALID_STATE_TRANSITION",
            "message": f"Only FAILED commands can be retried (state: {command.state.value})",
            "accepted": False})
    # §4.19 review repair: the retry is a NEW command through the SAME
    # acceptance contract — UNIQUE key (per attempt, not a fixed one),
    # admission gates applied, and the caller's execution path creates the
    # initial work (the command payload carries the full original intent).
    from backend.services.command_service import accept_command
    from backend.config.settings import settings as app_settings
    import time as _time
    receipt = await accept_command(
        command_type=command.command_type,
        owner_user_id=command.owner_user_id,
        organization_id=command.organization_id,
        actor_type="user", source="retry",
        idempotency_key=f"retry:{command_id}:{int(_time.time())}",
        request_digest=command.request_digest,
        payload=dict(command.payload or {}),
        run_id=None,   # the retry's execution path allocates its own run
        parent_command_id=command.command_id,
        parent_run_id=command.run_id,
        parent_task_id=command.parent_task_id,
        max_pending_per_user=app_settings.TENANT_MAX_PENDING_COMMANDS,
        max_wait_seconds=2 * 3600,
    )
    return receipt.to_dict()


@router.get("/{command_id}/events")
async def command_events(command_id: str, request: Request,
                         user: AuthenticatedUser = Depends(get_current_user)):
    """Authorized command event history (§4.19) — the outbox records for
    this command, newest first."""
    repo = command_service.get_command_repo()
    if repo is None:
        raise HTTPException(status_code=503, detail="Command lifecycle unavailable")
    command = await repo.get_command(command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="No such command")
    if command.owner_user_id != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")
    # §4.19 (C12): backend-neutral event history — works on both stores.
    rows = await repo.command_events(command_id, limit=100)
    for row in rows:
        if isinstance(row.get("payload_json"), str):
            import json as _json
            try:
                row["payload"] = _json.loads(row.pop("payload_json") or "{}")
            except ValueError:
                row["payload"] = {}
    return {"command_id": command_id, "events": rows, "count": len(rows)}
