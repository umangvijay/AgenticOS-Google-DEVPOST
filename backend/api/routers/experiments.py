"""Experiments API (Phase 13) — the controlled self-improvement loop.

The SERVICE enforces the permission separation deterministically (agents
cannot approve/promote/roll back; the proposer cannot approve their own
candidate; thresholds and critical constraints gate promotion). The
ROUTER additionally requires the evaluate/approve/promote/rollback
endpoints on an admin principal — two independent layers for §13.4.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
from backend.models.experiment import (
    ExperimentKind, ExperimentState, ExperimentThresholds)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/experiments", tags=["experiments"])


def _stage_flags(request: Request) -> dict:
    from backend.services.improvement_governance import improvement_stage_flags
    from backend.config.settings import settings as app_settings
    return improvement_stage_flags(app_settings)


def _service(request: Request):
    factory = getattr(request.app.state, "factory", None)
    repo = getattr(factory, "experiment_repo", None) if factory else None
    if repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "EXPERIMENTS_UNAVAILABLE",
            "message": "Experiments are not configured on this deployment"})
    from backend.services.improvement_service import ImprovementService
    return ImprovementService(repo)


def _require_admin(user: AuthenticatedUser) -> None:
    if not user.is_admin():
        raise HTTPException(status_code=403,
                            detail="Admin access required for this action")


def _public(exp) -> dict:
    return {
        "experiment_id": exp.experiment_id,
        "kind": exp.kind.value,
        "target": exp.target,
        "hypothesis": exp.hypothesis,
        "expected_benefit": exp.expected_benefit,
        "baseline_ref": exp.baseline_ref,
        "candidate_ref": exp.candidate_ref,
        "candidate_diff": exp.candidate_diff,
        "dataset_ref": exp.dataset_ref,
        "dataset_hash": exp.dataset_hash,
        "grading_hash": exp.grading_hash,
        "thresholds": json.loads(exp.thresholds_json or "{}"),
        "state": exp.state.value,
        "state_version": exp.state_version,
        "results": json.loads(exp.results_json or "{}"),
        "proposed_by": exp.proposed_by,
        "evaluated_by": exp.evaluated_by,
        "approved_by": exp.approved_by,
        "activated_by": exp.activated_by,
        "rolled_back_by": exp.rolled_back_by,
        "cohort_size": exp.cohort_size,
        "promoted_dataset_hash": exp.promoted_dataset_hash,
        "promoted_results": (
            json.loads(exp.promoted_results_json)
            if exp.promoted_results_json else None),
        "promoted_actor": exp.promoted_actor,
        "deployment_version": exp.deployment_version,
        "rollback_reason": exp.rollback_reason,
        "rejection_reason": exp.rejection_reason,
        "created_at": exp.created_at,
        "updated_at": exp.updated_at,
    }


class ProposeBody(BaseModel):
    kind: str
    target: str
    hypothesis: str
    expected_benefit: str
    baseline_ref: str
    candidate_ref: str
    dataset_ref: str
    dataset: object
    thresholds: dict = {}
    candidate_diff: str = ""


@router.get("")
async def list_experiments(request: Request,
                           user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    rows = await _service(request).repo.list_experiments()
    return {"experiments": [_public(r) for r in rows], "count": len(rows)}


@router.get("/{experiment_id}")
async def get_experiment(experiment_id: str, request: Request,
                         user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    exp = await _service(request).repo.get_experiment(experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return _public(exp)


@router.post("/propose")
async def propose(body: ProposeBody, request: Request,
                  user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    from backend.services.improvement_governance import (
        require_stage, check_open_experiment_bound, GovernanceError)
    require_stage(_stage_flags(request), "proposals")
    try:
        await check_open_experiment_bound(_service(request).repo,
                                          tenant_id=user.user_id)
        kind = ExperimentKind(body.kind)
        thresholds = ExperimentThresholds.from_dict(body.thresholds)
        exp = await _service(request).propose(
            kind=kind, target=body.target, hypothesis=body.hypothesis,
            expected_benefit=body.expected_benefit,
            baseline_ref=body.baseline_ref, candidate_ref=body.candidate_ref,
            dataset_ref=body.dataset_ref, dataset=body.dataset,
            thresholds=thresholds,
            proposer=f"user:{user.user_id}",
            candidate_diff=body.candidate_diff)
        return _public(exp)
    except GovernanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        from backend.services.improvement_service import ImprovementError
        if isinstance(exc, ImprovementError):
            raise HTTPException(status_code=422, detail=str(exc))
        raise


class EvaluateBody(BaseModel):
    run_case_endpoint: str = ""   # optional deployment executor hint
    cases_json: str = ""          # the PINNED cases (never invented)


@router.post("/{experiment_id}/evaluate")
async def evaluate(experiment_id: str, request: Request,
                   body: EvaluateBody = None,
                   user: AuthenticatedUser = Depends(get_current_user)):
    """§13.13: enqueue a DURABLE comparison for the pinned experiment —
    returns the job identity; workers execute the pinned cases and the
    results land in the experiment's evidence. The comparison EXECUTOR
    (the model path) is deployment-wired; without one the job QUEUES —
    the API never fabricates numbers."""
    _require_admin(user)
    from backend.services.improvement_governance import require_stage
    require_stage(_stage_flags(request), "comparisons")
    service = _service(request)
    experiment = await service.repo.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    if experiment.state not in (ExperimentState.PROPOSED,):
        raise HTTPException(status_code=409, detail=(
            f"Comparison starts from PROPOSED, not {experiment.state.value}"))
    factory = getattr(request.app.state, "factory", None)
    comparison_repo = getattr(factory, "comparison_repo", None) if factory else None
    if comparison_repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "COMPARISON_STORE_UNAVAILABLE",
            "message": "The comparison store is not configured"})
    import json as _json
    # The pinned cases come from the experiment's dataset snapshot; the
    # API passes them at proposal time — here we read the pinned hash and
    # accept the cases from the request body if supplied.
    cases = _json.loads(getattr(body, "cases_json", "[]") or "[]") \
        if body is not None and getattr(body, "cases_json", None) else []
    if not cases:
        raise HTTPException(status_code=422, detail=(
            "The comparison needs the pinned cases (cases_json) — the API "
            "never invents evaluation inputs"))
    # §14.J: the submitted cases must hash to the dataset PINNED at
    # proposal — a swapped or cherry-picked case set can never become
    # the evidence promotion reads. Grading cannot drift mid-flight.
    from backend.services.improvement_service import dataset_hash_for
    if dataset_hash_for(cases) != experiment.dataset_hash:
        raise HTTPException(status_code=409, detail=(
            "The submitted cases do not match the dataset hash pinned at "
            "proposal — the held-out set is immutable (§13.2/§14.J)"))
    # The gate verified the RAW pinned dataset (any shape); the
    # comparison pipeline executes a flat case LIST — unwrap the common
    # {"cases": [...]} envelope after the hash check.
    if isinstance(cases, dict) and isinstance(cases.get("cases"), list):
        cases = cases["cases"]
    revision_hash = _json.dumps({
        "dataset": experiment.dataset_hash,
        "grading": experiment.grading_hash,
        "baseline": experiment.baseline_ref,
        "candidate": experiment.candidate_ref}, sort_keys=True)
    from backend.services.experiment_comparison import ComparisonError
    job = await comparison_repo.enqueue(
        experiment_id, cases=cases, baseline_ref=experiment.baseline_ref,
        candidate_ref=experiment.candidate_ref, revision_hash=revision_hash)
    return {"comparison_id": job["comparison_id"], "state": job["state"],
            "experiment_id": experiment_id,
            "note": ("workers execute the pinned cases; results land in "
                     "the experiment evidence when the comparison "
                     "completes")}


@router.post("/{experiment_id}/shadow")
async def start_shadow(experiment_id: str, request: Request,
                       user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    try:
        exp = await _service(request).start_shadow(
            experiment_id, activator=f"user:{user.user_id}")
        return _public(exp)
    except Exception as exc:
        from backend.services.improvement_service import ImprovementError
        if isinstance(exc, ImprovementError):
            raise HTTPException(status_code=409, detail=str(exc))
        raise


class CanaryBody(BaseModel):
    cohort_size: int


@router.post("/{experiment_id}/canary")
async def start_canary(experiment_id: str, body: CanaryBody, request: Request,
                       user: AuthenticatedUser = Depends(get_current_user)):
    _require_admin(user)
    try:
        exp = await _service(request).start_canary(
            experiment_id, activator=f"user:{user.user_id}",
            cohort_size=body.cohort_size)
        return _public(exp)
    except Exception as exc:
        from backend.services.improvement_service import ImprovementError
        if isinstance(exc, ImprovementError):
            raise HTTPException(status_code=409, detail=str(exc))
        raise


class PromoteBody(BaseModel):
    deployment_version: str
    total_population: int = None
    expected_active_version_id: str = None   # §13.21 concurrency guard


@router.get("/specialization/compare")
async def compare_specialization(request: Request,
                                 user: AuthenticatedUser = Depends(get_current_user)):
    """§15.23 (finding E): the specialist-vs-baseline evaluation endpoint.
    The metrics come from the durable evaluation records (the caller
    supplies the measured windows); missing measurements stay MISSING.
    Read-only — the endpoint decides nothing."""
    _require_admin(user)
    from backend.services.specialization_eval import evaluate_specialization
    body_state = getattr(request.app.state, "specialization_metrics", None) or {}
    return evaluate_specialization(
        baseline=body_state.get("baseline", {}),
        specialist=body_state.get("specialist", {}),
        by_family=body_state.get("by_family"))


@router.post("/{experiment_id}/promote")
async def promote(experiment_id: str, body: PromoteBody, request: Request,
                  user: AuthenticatedUser = Depends(get_current_user)):
    """§13.19: promotion through the API ACTIVATES the candidate — the
    effective runtime changes (the deployment coordinator performs the
    gated promotion AND the activation, with a reconcilable receipt)."""
    _require_admin(user)
    from backend.services.improvement_governance import (
        require_stage, GovernanceError as _Gov)
    require_stage(_stage_flags(request), "activation")
    service = _service(request)
    factory = getattr(request.app.state, "factory", None)
    prompt_store = getattr(factory, "prompt_version_store", None) if factory else None
    try:
        if prompt_store is None:
            raise HTTPException(status_code=503, detail={
                "code": "PROMPT_STORE_UNAVAILABLE",
                "message": "Activation needs the versioned prompt store"})
        from backend.services.promotion_coordinator import (
            CoordinatorError, promote_and_activate,
        )
        out = await promote_and_activate(
            service, prompt_store, experiment_id,
            approver=f"user:{user.user_id}",
            deployment_version=body.deployment_version,
            total_population=body.total_population,
            expected_active_version_id=body.expected_active_version_id)
        result = _public(out["experiment"])
        result["activation_receipt"] = out["activation_receipt"]
        return result
    except HTTPException:
        raise
    except HTTPException:
        raise
    except Exception as exc:
        from backend.services.improvement_service import ImprovementError
        from backend.services.promotion_coordinator import CoordinatorError
        from backend.services.improvement_governance import (
            GovernanceError as _Gov)
        if isinstance(exc, (ImprovementError, CoordinatorError, _Gov)):
            raise HTTPException(status_code=409, detail=str(exc))
        raise


class RejectBody(BaseModel):
    reason: str = Field(default="", max_length=4000)


@router.post("/{experiment_id}/reject")
async def reject(experiment_id: str, request: Request,
                 user: AuthenticatedUser = Depends(get_current_user),
                 body: RejectBody | None = None):
    _require_admin(user)
    try:
        exp = await _service(request).reject(
            experiment_id, approver=f"user:{user.user_id}",
            reason=body.reason if body else "")
        return _public(exp)
    except Exception as exc:
        from backend.services.improvement_service import ImprovementError
        if isinstance(exc, ImprovementError):
            raise HTTPException(status_code=409, detail=str(exc))
        raise


class RollbackBody(BaseModel):
    reason: str


@router.post("/{experiment_id}/rollback")
async def rollback(experiment_id: str, body: RollbackBody, request: Request,
                   user: AuthenticatedUser = Depends(get_current_user)):
    """§13.20: API rollback RESTORES the pinned baseline via the
    coordinator — the response distinguishes rollback-REQUESTED from
    rollback-CONFIRMED (the effective runtime actually reverted)."""
    _require_admin(user)
    factory = getattr(request.app.state, "factory", None)
    prompt_store = getattr(factory, "prompt_version_store", None) if factory else None
    try:
        from backend.services.promotion_coordinator import (
            CoordinatorError, rollback_via_api,
        )
        if prompt_store is None:
            raise HTTPException(status_code=503, detail={
                "code": "PROMPT_STORE_UNAVAILABLE",
                "message": "Rollback needs the versioned prompt store"})
        out = await rollback_via_api(
            _service(request), prompt_store, experiment_id,
            operator=f"user:{user.user_id}", reason=body.reason)
        result = _public(out["experiment"])
        result["rollback_confirmed"] = out["rollback_confirmed"]
        return result
    except HTTPException:
        raise
    except HTTPException:
        raise
    except Exception as exc:
        from backend.services.improvement_service import ImprovementError
        from backend.services.promotion_coordinator import CoordinatorError
        from backend.services.improvement_governance import (
            GovernanceError as _Gov)
        if isinstance(exc, (ImprovementError, CoordinatorError, _Gov)):
            raise HTTPException(status_code=409, detail=str(exc))
        raise
