"""
AgentOS — Context / token usage

GET /api/v1/usage/context   — context-WINDOW estimator (§2.2: how much
                              material may occupy a model request; NOT spend)
GET /api/v1/usage/budget    — authoritative budget snapshot (Phase 2 §2.15)
POST /api/v1/usage/budget/check — admission probe with structured errors

The two are separate concepts: a large context estimate is not expenditure.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
from backend.config.settings import settings
from backend.agents.planner.planner_agent import CORE_NODES_DOC

router = APIRouter(prefix="/usage", tags=["usage"])

CHARS_PER_TOKEN = 4

# §27.D4: the context limit is a property of the RESOLVED MODEL, not a
# product-wide constant. Values are declared model capacities (Gemini
# 1M-token class for the flash/pro family; conservative fallback for
# unknown models). The workspace context BUDGET — how much of the window
# the product deliberately uses — stays an explicit policy value.
MODEL_CONTEXT_LIMITS = {
    "gemini-3.8-flash": 1_000_000,
    "gemini-3.7-flash": 1_000_000,
    "gemini-3.6-flash": 1_000_000,
    "gemini-3.5-flash": 1_000_000,
    "gemini-3.5-flash-lite": 1_000_000,
    "gemini-3.1-pro-preview": 2_000_000,
    "qwen2.5:3b-instruct": 32_768,
}
DEFAULT_CONTEXT_LIMIT = 128_000
CONTEXT_BUDGET_TOKENS = 256_000  # explicit product policy (may exceed the
# fallback for small models; the UI renders min(limit, budget) as usable)


def model_context_limit(model: str) -> int:
    return MODEL_CONTEXT_LIMITS.get(str(model or ""), DEFAULT_CONTEXT_LIMIT)


def _tokens(text: str) -> int:
    if not text:
        return 0
    return max(0, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def _get_factory(request: Request):
    return getattr(request.app.state, "factory", None)


def _run_blob(run: Dict[str, Any]) -> str:
    parts = [str(run.get("goal") or "")]
    for task in run.get("tasks") or []:
        parts.append(str(task.get("task_id") or ""))
        parts.append(json.dumps(task.get("output_data") or {}, default=str)[:4000])
        parts.append(str(task.get("error") or ""))
    return "\n".join(parts)


@router.get("/context")
async def get_context_usage(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Context-window estimator only (§2.2). Daily spending lives in
    GET /usage/budget — the authoritative ledger; this endpoint no longer
    pretends a rolling estimate is a spending period (§2.14)."""
    factory = _get_factory(request)
    tool_router = getattr(request.app.state, "tool_router", None)

    catalog: List[Any] = []
    if tool_router:
        try:
            catalog = await tool_router.get_tool_catalog(user.user_id) or []
        except Exception:
            catalog = []

    system_prompt = (
        "You are the Orchestrator Agent. Execute the given task using the best available capability. "
        "Build missing integrations, browse websites, send mail, check site health, debug code, "
        "generate projects, and create ATS resumes."
    )
    tool_blob = json.dumps(catalog, default=str)
    core_tools = (
        "call_external_tool build_integration browse_website send_email list_stored_credentials "
        "check_website_health debug_code generate_project create_resume analyze_resume_ats tailor_resume"
    )

    conversation = ""
    if factory:
        try:
            runs = await factory.workflow_repo.list_runs(user.user_id, limit=40, offset=0)
            blobs = []
            for run in runs[:8]:
                blobs.append(_run_blob(run))
            conversation = "\n\n".join(blobs)
        except Exception:
            conversation = ""

    memory_blob = ""
    model = settings.GEMINI_MODEL
    if factory:
        try:
            memories = await factory.memory_repo.list_memories(user.user_id, limit=8)
            if memories:
                memory_blob = "\n".join(str(m.get("content") or "") for m in memories)
        except Exception:
            memory_blob = ""
        try:
            user_settings = await factory.settings_repo.get_settings(user.user_id)
            if user_settings and user_settings.get("default_model"):
                model = str(user_settings["default_model"])
        except Exception:
            pass

    categories = [
        {"id": "system", "label": "System prompt", "tokens": _tokens(system_prompt)},
        {"id": "tools", "label": "Tool definitions", "tokens": _tokens(tool_blob) + _tokens(core_tools)},
        {"id": "mcp", "label": "MCP & dynamic tools", "tokens": _tokens(json.dumps([t.get("name") for t in catalog], default=str))},
        {"id": "subagents", "label": "Subagent definitions", "tokens": _tokens(CORE_NODES_DOC)},
        {"id": "conversation", "label": "Conversation", "tokens": _tokens(conversation)},
        {"id": "memory", "label": "Memory", "tokens": _tokens(memory_blob)},
    ]
    used = sum(c["tokens"] for c in categories)
    limit = model_context_limit(model)
    usable = min(limit, CONTEXT_BUDGET_TOKENS)
    remaining = max(0, usable - used)
    pct = min(100.0, round(100.0 * used / usable, 1)) if usable else 0

    return {
        # §27.D4: three separate quantities — never blended.
        "model_context_limit": limit,
        "context_budget": CONTEXT_BUDGET_TOKENS,
        "usable_tokens": usable,
        "window_tokens": usable,          # backward-compatible field
        "estimate_method": "chars/4 aggregate over system+tools+recent runs",
        "context_estimate": used,
        "remaining_tokens": remaining,
        "percent": pct,
        "model": model,
        "tool_count": len(catalog),
        "categories": categories,
        # Spending is a different concept with a different endpoint (§2.2).
        "budget_endpoint": "/api/v1/usage/budget",
        "note": ("Context estimate is not expenditure; see /usage/budget "
                 "for the authoritative ledger. The estimate is an "
                 "aggregate approximation of the NEXT assembled request."),
    }


# ── Authoritative budget snapshot (Phase 2, §2.15) ────────────────────


@router.get("/budget")
async def get_budget_snapshot(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The authoritative spending ledger for the caller: settled consumption,
    outstanding holds (incl. uncertain subset), and available allowance for
    the current UTC period. Unavailable accounting is reported as an
    explicit unavailable state — never a plausible zero (§2.15)."""
    from backend.services.budget_service import BudgetService, AccountingUnavailableError
    from backend.models.usage import utc_period_day

    service = BudgetService()
    factory = _get_factory(request)
    settings_repo = getattr(factory, "settings_repo", None) if factory else None
    try:
        snapshot = await service.get_user_snapshot(user.user_id, settings_repo)
        return snapshot.to_dict()
    except AccountingUnavailableError:
        period = utc_period_day()
        return {
            "scope": "user",
            "subject_id": user.user_id,
            "unit": "tokens",
            "period": period,
            "limit": None,
            "consumed": None,
            "reserved": None,
            "uncertain_reserved": None,
            "available": None,
            "accounting_status": "unavailable",
            "policy_version": None,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "message": "Usage information is temporarily unavailable. New paid work is paused.",
        }


class BudgetCheckRequest(BaseModel):
    required_units: int


@router.post("/budget/check")
async def check_budget_admission(
    body: BudgetCheckRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Admission probe: would a call needing `required_units` be admitted
    now? Returns structured errors (BUDGET_EXHAUSTED with available/required,
    ACCOUNTING_UNAVAILABLE) instead of plausible numbers (§2.15)."""
    from fastapi import HTTPException
    from backend.services.budget_service import BudgetService, AccountingUnavailableError
    from backend.models.usage import BudgetError, ERROR_HTTP_STATUS

    service = BudgetService()
    factory = _get_factory(request)
    settings_repo = getattr(factory, "settings_repo", None) if factory else None
    try:
        snapshot = await service.check_admission(
            user.user_id, int(body.required_units), settings_repo,
        )
        return {"admitted": True, "snapshot": snapshot.to_dict()}
    except BudgetError as exc:
        status_code = ERROR_HTTP_STATUS.get(exc.code, 400)
        raise HTTPException(status_code=status_code, detail=exc.to_dict())
    except AccountingUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ACCOUNTING_UNAVAILABLE", "retryable_now": False,
                    "message": "Budget accounting is unavailable; new paid work is paused"},
        ) from exc


@router.get('/operations')
async def get_operations(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    from fastapi import HTTPException
    from backend.services.operations_summary import summarize_runs
    factory = _get_factory(request)
    if not factory:
        raise HTTPException(status_code=503, detail='Workflow storage is unavailable')
    runs = await factory.workflow_repo.list_runs(user.user_id, limit=200, offset=0)
    summary = summarize_runs(runs, user.user_id)
    # Regression baselines + z-score anomalies from the same real history
    try:
        from backend.services.run_analytics import RunAnalytics
        summary["analytics"] = RunAnalytics(factory).analyze(runs)
    except Exception:
        summary["analytics"] = {"baselines": [], "anomalies": []}
    return summary

@router.get("/models")
async def get_model_leaderboard(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    """Model leaderboard from the adaptive selection policy — real per-model
    outcomes (attempts, success rate, EWMA latency) with the current UCB1
    ranking. This is the observable face of the self-evolving loop."""
    from backend.services.adaptive_policy import AdaptivePolicy
    factory = _get_factory(request)
    repo = getattr(factory, "adaptive_policy_repo", None) if factory else None
    policy = AdaptivePolicy(repo)
    board = await policy.leaderboard()
    return {
        "policy": "UCB1 (C=1.0) + epsilon=0.10 exploration + latency tie-break",
        "models": board,
        "note": "Routing policy learned from real task outcomes; no weights are trained.",
    }



@router.get("/routing/health")
async def routing_log_health(request: Request,
                             user: AuthenticatedUser = Depends(get_current_user)):
    """Phase 14 §14.4: the decision log's coverage health — decisions
    without feedback are EXPLICIT (never silently dropped)."""
    factory = _get_factory(request)
    repo = getattr(factory, "routing_repo", None) if factory else None
    if repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "ROUTING_LOG_UNAVAILABLE",
            "message": "The routing decision log is not configured"})
    return await repo.logging_health()


@router.get("/routing/compare")
async def routing_offline_comparison(request: Request,
                                     candidate_action: str,
                                     user: AuthenticatedUser = Depends(get_current_user)):
    """Phase 14 §14.5/§14.6/§14.22 (admin): offline comparison of a
    candidate fixed policy against the logging policy over the
    verified-feedback log. The result is DESCRIPTIVE unless the log
    carries stochastic propensities (`counterfactual_supported`) — this
    endpoint promotes nothing and certifies nothing."""
    # §14.22: the estimator identity travels with the numbers.
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")
    factory = _get_factory(request)
    repo = getattr(factory, "routing_repo", None) if factory else None
    if repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "ROUTING_LOG_UNAVAILABLE",
            "message": "The routing decision log is not configured"})
    from backend.services.offline_policy_evaluation import compare, fixed_policy
    decisions = await repo.logged_decisions()
    return compare(decisions, fixed_policy(candidate_action))


@router.get("/routing/summary")
async def routing_summary(request: Request,
                          user: AuthenticatedUser = Depends(get_current_user)):
    """Phase 14 §14.2 (admin): the acceptance's measured report — per
    task category: verified quality with a CI, cost, latency, and
    confidence. Read-only telemetry."""
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")
    factory = _get_factory(request)
    repo = getattr(factory, "routing_repo", None) if factory else None
    if repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "ROUTING_LOG_UNAVAILABLE",
            "message": "The routing decision log is not configured"})
    if not hasattr(repo, "summary_by_category"):
        raise HTTPException(status_code=503, detail={
            "code": "ROUTING_SUMMARY_UNSUPPORTED",
            "message": "The routing store does not support summaries"})
    return await repo.summary_by_category()


@router.get("/routing/drift")
async def routing_drift(request: Request,
                        user: AuthenticatedUser = Depends(get_current_user)):
    """Phase 14 §14.5: reward-distribution drift monitoring — recent vs
    long-run mean reward per context. A signal for operators; the policy
    does not auto-tune on it."""
    factory = _get_factory(request)
    repo = getattr(factory, "adaptive_policy_repo", None) if factory else None
    if repo is None or not hasattr(repo, "drift_windows"):
        raise HTTPException(status_code=503, detail={
            "code": "CONTEXTUAL_POLICY_UNAVAILABLE",
            "message": "The contextual routing policy is not configured"})
    from backend.services.contextual_policy import ContextualRoutingPolicy
    policy = ContextualRoutingPolicy(repo)
    return await policy.drift_report()
