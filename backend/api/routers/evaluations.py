"""Evaluation API (Phase 12).

Authorized retrieval of durable evaluation records — evidence-based run
outcomes with scorer version and fixture hash, plus the aggregate
summary (success rate, latency, cost, recoveries).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


def _repo(request: Request):
    factory = getattr(request.app.state, "factory", None)
    repo = getattr(factory, "evaluation_repo", None) if factory else None
    if repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "EVALUATIONS_UNAVAILABLE",
            "message": "Evaluation records are not configured on this deployment"})
    return repo


def _public(row: dict) -> dict:
    record = row.get("record") or {}
    return {
        "evaluation_id": row["evaluation_id"],
        "run_id": row.get("run_id"),
        "workflow_id": row.get("workflow_id"),
        "agent_name": row.get("agent_name"),
        "model": row.get("model"),
        "success": bool(row.get("success")),
        "total_tasks": record.get("total_tasks"),
        "completed_tasks": record.get("completed_tasks"),
        "failed_tasks": record.get("failed_tasks"),
        "recovery_attempts": row.get("recovery_attempts"),
        "successful_recoveries": record.get("successful_recoveries"),
        "tool_calls": row.get("tool_calls"),
        "unknown_tool_call_rate": row.get("unknown_tool_call_rate"),
        "invalid_argument_rate": row.get("invalid_argument_rate"),
        "tool_schema_violation_rate": row.get("tool_schema_violation_rate"),
        "latency_ms": row.get("latency_ms"),
        "token_usage_total": row.get("token_usage_total"),
        "uncertain_effects": record.get("uncertain_effects") or [],
        "failed_effects": record.get("failed_effects") or [],
        "confirmed_effects": record.get("confirmed_effects") or [],
        "task_verifications": record.get("task_verifications") or [],
        "fixture_hash": row.get("fixture_hash"),
        "evaluator_version": row.get("evaluator_version"),
        "created_at_epoch": row.get("created_at_epoch"),
    }


@router.get("/summary")
async def evaluation_summary(request: Request,
                             user: AuthenticatedUser = Depends(get_current_user)):
    """§12: aggregate metrics — success rate, latency, cost, recoveries.
    Admin-only: the summary spans all tenants."""
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")
    return await _repo(request).evaluation_summary()


@router.get("/runs/{run_id}")
async def evaluations_for_run(run_id: str, request: Request,
                              limit: int = 50, offset: int = 0,
                              user: AuthenticatedUser = Depends(get_current_user)):
    """§12.30: bounded, stably-ordered retrieval with evidence
    completeness per record (§12.18: missing evidence is first-class)."""
    repo = _repo(request)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    rows = await repo.evaluations_for_run(run_id, limit=limit, offset=offset)
    result = []
    for row in rows:
        record = row.get("record") or {}
        if record.get("user_id") != user.user_id and not user.is_admin():
            continue
        entry = _public(row)
        entry["evidence_completeness"] = {
            "gaps": record.get("evidence_gaps") or [],
            "unattributed_effects": record.get("unattributed_effects") or [],
            "measurement_quality": record.get("measurement_quality") or {},
        }
        result.append(entry)
    return {"run_id": run_id, "evaluations": result, "count": len(result),
            "limit": limit, "offset": offset}


@router.get("/{evaluation_id}")
async def get_evaluation(evaluation_id: str, request: Request,
                         user: AuthenticatedUser = Depends(get_current_user)):
    repo = _repo(request)
    row = await repo.get_evaluation(evaluation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    record = row.get("record") or {}
    if record.get("user_id") != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")
    return _public(row)


@router.get("/compare/{run_a}/{run_b}")
async def compare_runs_endpoint(run_a: str, run_b: str, request: Request,
                                user: AuthenticatedUser = Depends(get_current_user)):
    """§21.3: evidence-based comparison of two runs. Read-only — the
    runs' external effects are never re-executed."""
    repo = _repo(request)
    a = await repo.evaluations_for_run(run_a)
    b = await repo.evaluations_for_run(run_b)
    visible_a = [r for r in a if (r.get("record") or {}).get("user_id") in
                 (user.user_id, None) or user.is_admin()]
    visible_b = [r for r in b if (r.get("record") or {}).get("user_id") in
                 (user.user_id, None) or user.is_admin()]
    if not visible_a and not visible_b:
        raise HTTPException(status_code=404,
                            detail="No evaluations found for either run")
    from backend.services.workflow_templates import compare_runs
    return compare_runs(visible_a, visible_b,
                        label_a=run_a, label_b=run_b)
