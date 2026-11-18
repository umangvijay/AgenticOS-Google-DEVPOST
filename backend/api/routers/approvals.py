"""
AgentOS — Approvals Router

GET  /api/v1/approvals                 — List pending approvals for the user
POST /api/v1/approvals/{id}/approve    — Approve and resume the workflow task
POST /api/v1/approvals/{id}/reject     — Reject and fail the workflow task
"""

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from backend.models.security import ApprovalStatus
from backend.models.schemas import TaskTriggerEvent, TaskStatus
from backend.api.dependencies.auth import get_current_user, AuthenticatedUser
from backend.repositories.audit_repository import audit_repo, AuditEvent
from backend.engine.repo_adapter import maybe_await, load_run, persist_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _get_factory(request: Request):
    factory = getattr(request.app.state, "factory", None)
    if not factory:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return factory


def _approval_field(approval, key: str):
    if isinstance(approval, dict):
        return approval.get(key)
    return getattr(approval, key, None)


def _approval_expired(approval) -> bool:
    expiry = _approval_field(approval, "expires_at")
    if isinstance(expiry, str):
        try:
            expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except ValueError:
            return True
    if not isinstance(expiry, datetime):
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expiry


async def _get_waiting_task(repo, run_id: str, task_id: str):
    run = await load_run(repo, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Approval workflow run not found")
    task = next((item for item in run.tasks if item.task_id == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="Approval task not found")
    if task.status != TaskStatus.WAITING_APPROVAL:
        raise HTTPException(status_code=409, detail="Task is no longer waiting for approval")
    return run, task


@router.get("")
async def list_approvals(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List all pending approvals for the authenticated user."""
    factory = _get_factory(request)
    approvals = await maybe_await(factory.workflow_repo.list_pending_approvals(user.user_id))
    return {"approvals": approvals, "count": len(approvals)}


@router.post("/{approval_id}/approve")
async def approve_request(
    approval_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Approve a pending request and resume the workflow."""
    factory = _get_factory(request)
    repo = factory.workflow_repo

    approval = await maybe_await(repo.get_approval(approval_id))
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")

    if _approval_field(approval, "user_id") != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Not authorized to access this approval")

    # §4.14: the actor's CURRENT authority is rechecked at decision time —
    # a disabled account cannot approve work (acceptance never grants
    # permanent permission).
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    run_id = _approval_field(approval, "run_id")
    task_id = _approval_field(approval, "task_id")
    workflow_id = _approval_field(approval, "workflow_id")
    run, task = await _get_waiting_task(repo, run_id, task_id)
    if _approval_expired(approval):
        await maybe_await(repo.resolve_approval(
            approval_id, ApprovalStatus.EXPIRED.value, decision_by="SYSTEM"
        ))
        raise HTTPException(status_code=410, detail="Approval request has expired")

    success = await maybe_await(
        repo.resolve_approval(approval_id, ApprovalStatus.APPROVED.value, decision_by=user.user_id)
    )
    if not success:
        raise HTTPException(status_code=409, detail="Approval already resolved or not pending")

    # Transition the task back to PENDING with the approval bound to it
    await factory.idempotency_repo.reset_failed_task(workflow_id, task_id)
    task.status = TaskStatus.PENDING
    task.error = None
    task.input_data["_approved_request_id"] = approval_id
    await persist_task(repo, run.run_id, task)

    from backend.repositories.audit_repository import ActorType
    audit_repo.log_event(AuditEvent(
        event_type="APPROVAL_GRANTED",
        actor_id=user.user_id,
        actor_type=ActorType.USER,
        resource_id=approval_id,
        workflow_id=workflow_id,
        run_id=run_id,
        task_id=task_id,
        details={"workflow_id": workflow_id, "task_id": task_id}
    ))

    await maybe_await(repo.update_run_status(run_id, "RUNNING"))
    event = TaskTriggerEvent(workflow_id=workflow_id, run_id=run_id, task_id=task_id)
    await factory.message_bus.publish("agentos-workflow-events", event)

    return {"status": "success", "message": "Approval granted"}


@router.post("/{approval_id}/reject")
async def reject_request(
    approval_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Reject a pending request, causing the workflow task to fail."""
    factory = _get_factory(request)
    repo = factory.workflow_repo

    approval = await maybe_await(repo.get_approval(approval_id))
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")

    if _approval_field(approval, "user_id") != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Not authorized to access this approval")

    # §4.14: the actor's CURRENT authority is rechecked at decision time —
    # a disabled account cannot approve work (acceptance never grants
    # permanent permission).
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    run_id = _approval_field(approval, "run_id")
    task_id = _approval_field(approval, "task_id")
    workflow_id = _approval_field(approval, "workflow_id")
    run, task = await _get_waiting_task(repo, run_id, task_id)
    if _approval_expired(approval):
        await maybe_await(repo.resolve_approval(
            approval_id, ApprovalStatus.EXPIRED.value, decision_by="SYSTEM"
        ))
        raise HTTPException(status_code=410, detail="Approval request has expired")

    success = await maybe_await(
        repo.resolve_approval(approval_id, ApprovalStatus.REJECTED.value, decision_by=user.user_id)
    )
    if not success:
        raise HTTPException(status_code=409, detail="Approval already resolved or not pending")

    task.status = TaskStatus.FAILED
    task.error = "Human approval rejected"
    await persist_task(repo, run.run_id, task)

    from backend.repositories.audit_repository import ActorType
    audit_repo.log_event(AuditEvent(
        event_type="APPROVAL_REJECTED",
        actor_id=user.user_id,
        actor_type=ActorType.USER,
        resource_id=approval_id,
        workflow_id=workflow_id,
        run_id=run_id,
        task_id=task_id,
        details={"workflow_id": workflow_id, "task_id": task_id}
    ))

    # Cascade the failure to downstream tasks
    workflow_engine = getattr(request.app.state, "workflow_engine", None)
    if workflow_engine:
        await workflow_engine.evaluate_dag(run_id)

    return {"status": "success", "message": "Approval rejected"}
