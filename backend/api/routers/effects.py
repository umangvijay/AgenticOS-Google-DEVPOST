"""Effects & reconciliation API (Phase 5 §5.19).

Every endpoint checks CURRENT resource authorization — knowing an effect ID
is not permission (§5.19). Resolutions are evidence-backed and conditional;
there is deliberately NO raw "set completed/failed" endpoint (§5.11).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
from backend.models.effect import EffectState
from backend.services import reconciliation_service
from backend.repositories.effect_repository import get_effect_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/effects", tags=["effects"])


def _repo():
    repo = get_effect_repo()
    if repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "EFFECTS_UNAVAILABLE",
            "message": "The effect ledger is not configured on this deployment"})
    return repo


async def _authorized_effect(effect_id: str, user: AuthenticatedUser,
                             request: Request):
    repo = _repo()
    effect = await repo.get_effect(effect_id)
    if effect is None:
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND", "message": "No such effect"})
    if effect.owner_user_id != user.user_id and not user.is_admin():
        raise HTTPException(status_code=403, detail="Access denied")
    return effect


def _summary(effect) -> dict:
    """§5.19: what the UI explains — attempted/target/confirmed/unknown and
    which actions are permitted. Evidence bodies stay redacted summaries."""
    # §27.19: both repository shapes reach this router — SQLite returns
    # Effect models, Postgres returns shaped dicts. Normalize once.
    if isinstance(effect, dict):
        state = str(effect.get("state") or "")
        terminal = bool(effect.get("terminal"))
        can_retry = state == EffectState.CONFIRMED_NOT_APPLIED.value
        return {
            "effect_id": effect.get("effect_id"),
            "operation": effect.get("operation_name"),
            "state": state,
            "state_version": effect.get("state_version"),
            "target": effect.get("target"),
            "run_id": effect.get("run_id"),
            "task_id": effect.get("task_id"),
            "terminal": terminal,
            "permitted_actions": {
                "reconcile": not terminal,
                "retry": can_retry,
                "compensate": state == EffectState.CONFIRMED_APPLIED.value,
                "close_unresolved": state == EffectState.OUTCOME_UNKNOWN.value,
            },
        }
    can_retry = effect.state is EffectState.CONFIRMED_NOT_APPLIED
    return {
        "effect_id": effect.effect_id,
        "operation": effect.operation_name,
        "state": effect.state.value,
        "state_version": effect.state_version,
        "target": effect.target,
        "run_id": effect.run_id,
        "task_id": effect.task_id,
        "terminal": effect.terminal,
        "permitted_actions": {
            "reconcile": not effect.terminal,
            "retry": can_retry,   # §5.16: only confirmed-not-applied items
            "compensate": effect.state is EffectState.CONFIRMED_APPLIED,
            "close_unresolved": effect.state is EffectState.OUTCOME_UNKNOWN,
        },
    }


@router.get("/{effect_id}")
async def get_effect(effect_id: str, request: Request,
                     user: AuthenticatedUser = Depends(get_current_user)):
    effect = await _authorized_effect(effect_id, user, request)
    repo = _repo()
    return {**_summary(effect),
            "receipts": await repo.receipts_for(effect_id)}


@router.get("/{effect_id}/history")
async def effect_history(effect_id: str, request: Request,
                         user: AuthenticatedUser = Depends(get_current_user)):
    """§5.19: attempts, receipts, and resolutions for the effect."""
    effect = await _authorized_effect(effect_id, user, request)
    repo = _repo()
    receipts = await repo.receipts_for(effect_id)
    return {"effect_id": effect_id, "state": effect.state.value,
            "state_version": effect.state_version,
            "receipts": receipts,
            "created_at": effect.created_at, "updated_at": effect.updated_at}


@router.get("/by-run/{run_id}")
async def get_effects_by_run(run_id: str, request: Request,
                             user: AuthenticatedUser = Depends(get_current_user)):
    """C15-equivalent for effects: the workspace knows the run id — list the
    external effects it caused (safe summaries only)."""
    repo = _repo()
    # §27.19: the by-run listing goes through the REPOSITORY API (both
    # SQLite and Postgres provide effects_for_run) — raw SQLite-style
    # SQL here broke on PostgreSQL with a syntax error, 500ing the
    # workspace's own per-run effect view.
    effects = await repo.effects_for_run(run_id)
    out = []
    for effect in list(effects)[:20]:
        if effect is None:
            continue
        owner = (effect.get("owner_user_id")
                 if isinstance(effect, dict)
                 else getattr(effect, "owner_user_id", None))
        if owner != user.user_id and not user.is_admin():
            continue
        out.append(_summary(effect))
    return {"run_id": run_id, "effects": out, "count": len(out)}


@router.post("/{effect_id}/reconcile")
async def reconcile(effect_id: str, request: Request,
                    user: AuthenticatedUser = Depends(get_current_user)):
    """§5.10: request ONE bounded investigation — evidence-based, never a
    guess; insufficient evidence preserves OUTCOME_UNKNOWN."""
    effect = await _authorized_effect(effect_id, user, request)
    record = await reconciliation_service.reconcile_effect(_repo(), effect_id)
    effect = await _repo().get_effect(effect_id)
    return {**record, "effect": _summary(effect)}


class ResolveBody(dict):
    pass


@router.post("/{effect_id}/resolve")
async def resolve(effect_id: str, request: Request,
                  user: AuthenticatedUser = Depends(get_current_user)):
    """§5.11/§5.12: authorized conditional resolution. Evidence references
    are REQUIRED for confirmed outcomes; a decision without evidence can
    only close the effect as unresolved."""
    effect = await _authorized_effect(effect_id, user, request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")
    try:
        resolution = EffectState(body.get("resolution", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown resolution state")
    result = await reconciliation_service.resolve_effect(
        _repo(), effect_id,
        expected_version=int(body.get("expected_version", effect.state_version)),
        resolution=resolution,
        evidence_references=list(body.get("evidence_references") or []),
        actor=user.user_id,
        reason=str(body.get("reason") or ""))
    if not result.get("resolved"):
        status = 404 if result.get("error") == "NOT_FOUND" else 409
        raise HTTPException(status_code=status, detail=result)
    effect = await _repo().get_effect(effect_id)
    return {**result, "effect": _summary(effect)}
