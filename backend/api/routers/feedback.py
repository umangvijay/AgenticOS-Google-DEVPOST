"""Real-user feedback (§21.21): submission with traceability + explicit
diagnostic consent, authorized triage, retention, and deletion.
Feedback NEVER becomes a training reward or an authorization input."""
from __future__ import annotations

from typing import List, Optional
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import (
    AuthenticatedUser, get_current_user, require_admin, require_not_viewer)

router = APIRouter(prefix="/feedback", tags=["feedback"])

VALID_CATEGORIES = ("incorrect_result", "failed_action", "slow_run",
                    "missing_app", "confusing_ui", "other")
TRIAGE_STATES = ("received", "investigating", "planned", "fixed", "closed")


class FeedbackIn(BaseModel):
    helpful: bool
    category: str
    comment: str = Field(default="", max_length=4000)
    expected_result: str = Field(default="", max_length=2000)
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    tool_id: Optional[str] = None
    deliverable_revision: Optional[str] = None
    share_diagnostics: bool = False


def _repo(request: Request):
    factory = getattr(request.app.state, "factory", None)
    repo = getattr(factory, "feedback_repo", None)
    if repo is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Feedback store unavailable")
    return repo


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_feedback(body: FeedbackIn,
                          request: Request,
                          user: AuthenticatedUser = Depends(get_current_user)):
    if body.category not in VALID_CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"category must be one of {VALID_CATEGORIES}")
    repo = _repo(request)
    factory = request.app.state.factory
    run = {}
    if body.task_id and not body.run_id:
        raise HTTPException(400, "task_id requires run_id")
    if body.run_id:
        run = await factory.workflow_repo.get_run(body.run_id)
        if not run or run.get("user_id") != user.user_id:
            raise HTTPException(404, "Workflow not found")
        if body.task_id and not any(t.get("task_id") == body.task_id
                                    for t in run.get("tasks", [])):
            raise HTTPException(404, "Task not found in workflow")
    if body.tool_id:
        tool = await factory.mcp_repo.get_mcp(body.tool_id)
        if not tool or tool.get("owner") not in (user.user_id, "system"):
            raise HTTPException(404, "Tool not found")
    if body.deliverable_revision:
        # Revision ownership needs its own authoritative lookup; never accept
        # an unvalidated diagnostic reference merely because the user sent it.
        raise HTTPException(422, "Attach feedback to its workflow; direct deliverable revision references are not supported")
    from backend.security.shared_rate_limiter import current_window
    rate_repo = getattr(factory, "shared_rate_repo", None)
    if rate_repo is None:
        raise HTTPException(503, "Feedback admission store unavailable")
    try:
        admitted = await rate_repo.admit(user.user_id, "feedback", current_window(3600),
                                         50, window_seconds=3600)
    except Exception as exc:
        raise HTTPException(503, "Feedback admission store unavailable") from exc
    if not admitted.allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Feedback limit reached; retry after the current hour",
                            headers={"Retry-After": str(max(1, admitted.window_end - int(time.time())))})
    feedback_id = await repo.submit({
        "user_id": user.user_id,
        "helpful": body.helpful,
        "category": body.category,
        "comment": body.comment,
        "expected_result": body.expected_result,
        "run_id": body.run_id,
        "task_id": body.task_id,
        "tool_id": body.tool_id,
        "deliverable_revision": body.deliverable_revision,
        "model_version": run.get("model_version") or run.get("model"),
        "policy_version": run.get("policy_version"),
        "app_version": request.app.version,
        "share_diagnostics": body.share_diagnostics,
    })
    return {"feedback_id": feedback_id, "triage_status": "received"}


@router.get("/mine")
async def my_feedback(request: Request, run_id: Optional[str] = None,
                      user: AuthenticatedUser = Depends(get_current_user)):
    items = await _repo(request).list_for_user(user.user_id, run_id=run_id)
    # The user sees the triage status of their own reports.
    return {"feedback": [
        {"id": it["id"], "helpful": it["helpful"], "run_id": it.get("run_id"),
         "category": it["category"],
         "triage_status": it["triage_status"],
         "created_at": str(it.get("created_at"))}
        for it in items if run_id is None or it.get("run_id") == run_id]}


@router.get("")
async def triage_queue(status_filter: Optional[str] = None,
                       request: Request = None,
                       user: AuthenticatedUser = Depends(require_admin)):
    repo = _repo(request)
    items = await repo.list_for_triage(status_filter)
    # Diagnostics attached only with the user's explicit consent.
    return {"feedback": [
        {"id": it["id"], "helpful": it["helpful"],
         "category": it["category"], "comment": it["comment"],
         "expected_result": it["expected_result"],
         "run_id": it["run_id"] if it["share_diagnostics"] else None,
         "task_id": it["task_id"] if it["share_diagnostics"] else None,
         "share_diagnostics": it["share_diagnostics"],
         "triage_status": it["triage_status"],
         "triage_note": it["triage_note"]}
        for it in items]}


class TriageUpdate(BaseModel):
    status: str
    note: str = Field(default="", max_length=2000)


@router.post("/{feedback_id}/triage")
async def triage(feedback_id: str, body: TriageUpdate,
                 request: Request,
                 user: AuthenticatedUser = Depends(require_admin)):
    if body.status not in TRIAGE_STATES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"status must be one of {TRIAGE_STATES}")
    ok = await _repo(request).set_triage(feedback_id, body.status,
                                         user.user_id, body.note)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Feedback not found")
    return {"feedback_id": feedback_id, "triage_status": body.status}


@router.delete("/{feedback_id}")
async def delete_feedback(feedback_id: str, request: Request,
                          user: AuthenticatedUser = Depends(get_current_user)):
    """The USER can delete their own feedback (retention/deletion §21.21)."""
    ok = await _repo(request).delete_user_feedback(user.user_id, feedback_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Feedback not found")
    return {"deleted": feedback_id}
