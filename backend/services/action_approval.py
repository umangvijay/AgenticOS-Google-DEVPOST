"""Shared human-approval gate for side-effecting actions outside ToolRouter."""

from typing import Any, Dict

from backend.models.security import AutonomyLevel, RiskLevel, ApprovalRequest
from backend.services.approvals_engine import ApprovalsEngine, ApprovalRequiredException


def require_approval_for_action(
    *,
    context: Dict[str, Any],
    user_id: str,
    tool_name: str,
    arguments: Dict[str, Any],
    risk_level: RiskLevel = RiskLevel.HIGH,
    tool_version: str = "1",
) -> None:
    """Require a persisted, exact-argument approval according to autonomy policy.

    An approved request is accepted only for the same actor, workflow, run, task,
    tool and complete argument payload. Any mismatch creates a fresh approval.
    """
    ctx = context or {}
    autonomy = ctx.get("autonomy_level", AutonomyLevel.L1_SUPERVISED)
    if not isinstance(autonomy, AutonomyLevel):
        autonomy = AutonomyLevel(int(autonomy))

    engine = ApprovalsEngine()
    if not engine.requires_approval(autonomy, risk_level):
        return

    approved = ctx.get("approved_request")
    if isinstance(approved, dict):
        try:
            approved = ApprovalRequest(**approved)
        except Exception:
            approved = None
    if approved and (
        approved.user_id == user_id
        and approved.tool_name == tool_name
        and approved.tool_version == tool_version
        and approved.workflow_id == ctx.get("workflow_id")
        and approved.run_id == ctx.get("run_id")
        and approved.task_id == ctx.get("task_id")
        and approved.arguments == arguments
    ):
        try:
            engine.validate_approval_for_execution(approved, arguments)
            return
        except ValueError:
            # Expired, unresolved, or invalidated approvals never authorize an
            # action; request a new decision bound to the current payload.
            pass

    pending = engine.create_approval_request(
        user_id=user_id,
        tool_name=tool_name,
        tool_version=tool_version,
        risk_level=risk_level,
        autonomy_level=autonomy,
        arguments=arguments,
        workflow_id=ctx.get("workflow_id", "unknown"),
        run_id=ctx.get("run_id", "unknown"),
        task_id=ctx.get("task_id", "unknown"),
    )
    raise ApprovalRequiredException("Human approval is required for this action.", pending)
