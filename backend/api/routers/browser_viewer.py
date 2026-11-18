"""Browser session viewer/takeover API (Phase 20 §20.16).

Server-enforced ownership: every route resolves the session through the
BrowserExecutionService and refuses actors that do not OWN the session.
Viewing is read-only attachment; takeover is an explicit transfer that
invalidates the previous controller epoch (stale targets after a
handoff are refused downstream, per §15.20)."""
from __future__ import annotations

import hmac
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import (
    AuthenticatedUser, get_current_user, require_not_viewer)
from backend.services.browser_execution import (
    BrowserExecutionError, BrowserExecutionService, NotControllerError)

router = APIRouter(prefix="/browser", tags=["browser-viewer"])

# ONE process-wide service; sessions live with the process that owns
# the Playwright handles (worker pool in production).
_service: Optional[BrowserExecutionService] = None


def get_browser_service() -> BrowserExecutionService:
    global _service
    if _service is None:
        import os
        # CONTROLLED local journeys (§22.15/22.16): the compose core
        # profile explicitly opts in to private-network browser targets
        # for its hosted controlled app. Production/cloud deployments
        # leave this unset — private destinations stay prohibited.
        allow_private = os.environ.get(
            "BROWSER_ALLOW_PRIVATE_NETWORK", "").lower() in ("1", "true")
        _service = BrowserExecutionService(
            headless=True, allow_private_network=allow_private)
    return _service


def set_browser_service(service: Optional[BrowserExecutionService]) -> None:
    """Test/worker wiring."""
    global _service
    _service = service


class OpenSessionRequest(BaseModel):
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    allowed_origins: list[str] = Field(min_length=1)


class ExecuteRequest(BaseModel):
    action: str = Field(min_length=1)
    target: str = ""
    text: str = ""
    controller_epoch: str = Field(min_length=1, max_length=128)


def _handle_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotControllerError):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, BrowserExecutionError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.post("/sessions")
async def open_session(req: OpenSessionRequest, request: Request,
                       user: AuthenticatedUser = Depends(require_not_viewer)):
    factory = getattr(request.app.state, "factory", None)
    if factory is None:
        raise HTTPException(503, "Workflow store unavailable")
    run = await factory.workflow_repo.get_run(req.run_id)
    if not run or run.get("user_id") != user.user_id:
        raise HTTPException(404, "Workflow not found")
    if not any(t.get("task_id") == req.task_id for t in run.get("tasks", [])):
        raise HTTPException(404, "Task not found in workflow")
    service = get_browser_service()
    try:
        session = await service.open_session(
            owner_user_id=user.user_id, run_id=req.run_id,
            task_id=req.task_id, allowed_origins=req.allowed_origins,
            controller_worker_id=f"user:{user.user_id}")
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)
    return {"session_id": session.session_id,
            "controller_epoch": session.controller_epoch,
            "allowed_origins": session.allowed_origins}


@router.get("/sessions")
async def list_sessions(
        user: AuthenticatedUser = Depends(get_current_user)):
    return {"sessions": await get_browser_service().list_sessions(
        user.user_id)}


@router.get("/sessions/{session_id}")
async def session_state(session_id: str,
                        user: AuthenticatedUser = Depends(get_current_user)):
    """§25.3: routed through the service so another replica's sessions
    resolve across workers."""
    try:
        return await get_browser_service().session_state_for(
            user.user_id, session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@router.post("/sessions/{session_id}/view")
async def begin_viewing(session_id: str,
                        user: AuthenticatedUser = Depends(get_current_user)):
    try:
        return await get_browser_service().begin_viewing(
            user.user_id, session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@router.post("/sessions/{session_id}/takeover")
async def request_takeover(session_id: str,
                           user: AuthenticatedUser = Depends(require_not_viewer)):
    try:
        return await get_browser_service().request_takeover(
            user.user_id, session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@router.post("/sessions/{session_id}/execute")
async def execute_action(session_id: str, req: ExecuteRequest,
                         user: AuthenticatedUser = Depends(require_not_viewer)):
    try:
        result = await get_browser_service().execute(
            user.user_id, session_id, req.action, req.target, req.text,
            controller_epoch=req.controller_epoch,
            actor_controller_id=f"user:{user.user_id}")
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)
    return {"action": result.action, "target": result.target,
            "status": result.status, "executed": result.executed,
            "detail": result.detail[:500], "error": result.error,
            "url_after": result.url_after,
            "screenshot_sha256": result.screenshot_sha256,
            "duration_ms": result.duration_ms}


@router.post("/sessions/{session_id}/pause")
async def pause_session(session_id: str,
                        user: AuthenticatedUser = Depends(require_not_viewer)):
    try:
        return await get_browser_service().pause(user.user_id, session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@router.post("/sessions/{session_id}/resume")
async def resume_session(session_id: str,
                         user: AuthenticatedUser = Depends(require_not_viewer)):
    try:
        return await get_browser_service().resume(user.user_id, session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@router.post("/sessions/{session_id}/return-control")
async def return_control_session(session_id: str,
                                 user: AuthenticatedUser = Depends(require_not_viewer)):
    try:
        return await get_browser_service().return_control(
            user.user_id, session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@router.get("/sessions/{session_id}/frame")
async def session_frame(session_id: str,
                        user: AuthenticatedUser = Depends(get_current_user)):
    """THE LIVE VIEW: a real screenshot of the agent's current page."""
    try:
        return await get_browser_service().capture_frame(
            user.user_id, session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@router.delete("/sessions/{session_id}")
async def close_session(session_id: str,
                        user: AuthenticatedUser = Depends(require_not_viewer)):
    try:
        await get_browser_service().close_session(user.user_id, session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)
    return {"closed": session_id}


# ── §24.6 (R3): the worker-internal routing contract ────────────────────
# Another API replica proxies a command here when the durable registry
# names THIS worker as the session owner. The SAME server-side
# epoch/actor validation applies — routing never bypasses enforcement.
class InternalCommand(BaseModel):
    session_id: str = Field(min_length=1)
    actor_user_id: str = Field(min_length=1)


class InternalExecuteCommand(InternalCommand):
    action: str = Field(min_length=1)
    target: str = ""
    text: str = ""
    controller_epoch: str = ""
    actor_controller_id: str = "agent"


def _require_internal_token(request: Request) -> None:
    expected = (os.environ.get("AGENTOS_BROWSER_INTERNAL_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "internal browser routing is not configured")
    presented = (request.headers.get("x-internal-token") or "").strip()
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "invalid internal routing token")


internal_router = APIRouter(prefix="/internal/browser",
                            tags=["browser-internal"])


@internal_router.post("/view")
async def internal_view(cmd: InternalCommand, request: Request):
    _require_internal_token(request)
    try:
        return await get_browser_service().begin_viewing(
            cmd.actor_user_id, cmd.session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@internal_router.post("/execute")
async def internal_execute(cmd: InternalExecuteCommand, request: Request):
    _require_internal_token(request)
    try:
        result = await get_browser_service().execute(
            cmd.actor_user_id, cmd.session_id, cmd.action,
            target=cmd.target, text=cmd.text,
            controller_epoch=cmd.controller_epoch,
            actor_controller_id=cmd.actor_controller_id)
        from dataclasses import asdict
        return asdict(result)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@internal_router.post("/takeover")
async def internal_takeover(cmd: InternalCommand, request: Request):
    _require_internal_token(request)
    try:
        return await get_browser_service().request_takeover(
            cmd.actor_user_id, cmd.session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@internal_router.post("/frame")
async def internal_frame(cmd: InternalCommand, request: Request):
    _require_internal_token(request)
    try:
        return await get_browser_service().capture_frame(
            cmd.actor_user_id, cmd.session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@internal_router.post("/return-control")
async def internal_return(cmd: InternalCommand, request: Request):
    _require_internal_token(request)
    try:
        return await get_browser_service().return_control(
            cmd.actor_user_id, cmd.session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@internal_router.post("/state")
async def internal_state(cmd: InternalCommand, request: Request):
    """§25.3: cross-replica session STATE (the workspace panel polls it)."""
    _require_internal_token(request)
    try:
        return await get_browser_service().session_state_for(
            cmd.actor_user_id, cmd.session_id)
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@internal_router.post("/close")
async def internal_close(cmd: InternalCommand, request: Request):
    _require_internal_token(request)
    try:
        await get_browser_service().close_session(
            cmd.actor_user_id, cmd.session_id)
        return {"closed": cmd.session_id}
    except (BrowserExecutionError, NotControllerError) as exc:
        raise _handle_error(exc)


@internal_router.post("/pause")
async def internal_pause(cmd: InternalCommand, request: Request):
    _require_internal_token(request)
    try:
        return await get_browser_service().pause(cmd.actor_user_id, cmd.session_id)
    except BrowserExecutionError as exc:
        raise _handle_error(exc) from exc


@internal_router.post("/resume")
async def internal_resume(cmd: InternalCommand, request: Request):
    _require_internal_token(request)
    try:
        return await get_browser_service().resume(cmd.actor_user_id, cmd.session_id)
    except BrowserExecutionError as exc:
        raise _handle_error(exc) from exc
