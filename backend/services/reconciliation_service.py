"""Evidence-based reconciliation service (Phase 5 §5.10/§5.11/§5.12).

The bounded loop (§5.18): observe evidence → classify the unresolved
condition → choose an allowed investigation → execute it → verify → persist
the result. Absence of evidence is NOT proof of non-execution (§5.10) —
adapters define what evidence suffices for CONFIRMED_NOT_APPLIED, and
insufficient evidence preserves OUTCOME_UNKNOWN.

Resolutions are CONDITIONAL and auditable (§5.11): expected state+version,
evidence references, resolver identity, structured reason. An operator
decision is distinct from confirmed evidence (§5.12): a guess can close an
effect as CLOSED_UNRESOLVED but can never manufacture CONFIRMED_NOT_APPLIED
(that would enable unsafe automatic replay).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from backend.models.effect import (
    Effect,
    EffectState,
    EffectTransitionError,
    new_reconciliation_id,
)
from backend.models.admission import epoch_now

logger = logging.getLogger(__name__)

METRICS: Dict[str, int] = {}


def reconciliation_metrics() -> Dict[str, int]:
    return dict(METRICS)


# Evidence strategies (§5.10), registered per provider/operation. Each
# strategy returns (observations: dict, resolution: Optional[EffectState]):
# resolution=None means "insufficient evidence — remain unknown".
_STRATEGIES: Dict[str, Callable[[Effect], AwaitableLike]] = {}


class AwaitableLike:
    pass


def register_evidence_strategy(operation_name: str,
                               strategy) -> None:
    """Register an operation-specific evidence strategy:
    `async def strategy(effect) -> (observations: dict, resolution: EffectState | None)`.
    Deterministic local simulators register here first (§5.20)."""
    _STRATEGIES[operation_name] = strategy


def evidence_strategy_for(operation_name: str):
    return _STRATEGIES.get(operation_name)


async def reconcile_effect(effect_repo, effect_id: str, *,
                           actor: str = "reconciler",
                           strategy=None) -> Dict[str, Any]:
    """One bounded investigation (§5.10 steps 1-9). Returns the
    reconciliation record; the effect's state moves ONLY on sufficient
    evidence."""
    reconciliation_id = new_reconciliation_id()
    effect = await effect_repo.get_effect(effect_id)
    if effect is None:
        return {"reconciliation_id": reconciliation_id, "found": False}
    if effect.terminal:
        return {"reconciliation_id": reconciliation_id,
                "already_terminal": effect.state.value}
    # §5.13: an authorized reconciler may INSPECT after the user loses
    # execution permission — investigation grants no repeat permission.

    strategy = strategy or evidence_strategy_for(effect.operation_name)
    observations: Dict[str, Any]
    resolution: Optional[EffectState] = None
    if strategy is None:
        observations = {"note": "no evidence strategy registered for this operation"}
    else:
        observations, resolution = await strategy(effect)

    record: Dict[str, Any] = {
        "reconciliation_id": reconciliation_id,
        "effect_id": effect_id,
        "state_before": effect.state.value,
        "observations": observations,
        "resolution": resolution.value if resolution else None,
        "resolved": False,
    }
    if resolution is not None:
        try:
            await effect_repo.transition(
                effect_id, resolution,
                actor=actor,
                reason=f"reconciliation {reconciliation_id}: evidence-based")
            record["resolved"] = True
            METRICS[f"reconciled_{resolution.value}"] = \
                METRICS.get(f"reconciled_{resolution.value}", 0) + 1
        except EffectTransitionError as exc:
            record["conflict"] = str(exc)   # §5.20: concurrent resolvers race
            METRICS["reconciliation_conflicts"] = \
                METRICS.get("reconciliation_conflicts", 0) + 1
    else:
        METRICS["reconciliation_unknown_preserved"] = \
            METRICS.get("reconciliation_unknown_preserved", 0) + 1
    return record


async def resolve_effect(effect_repo, effect_id: str, *,
                         expected_version: int,
                         resolution: EffectState,
                         evidence_references: list,
                         actor: str,
                         reason: str) -> Dict[str, Any]:
    """§5.11/§5.12: an AUTHORIZED, conditional resolution. Evidence-backed
    confirmations change the outcome; an operator decision without evidence
    can only CLOSE_UNRESOLVED (never CONFIRMED_NOT_APPLIED — §5.12's
    unsafe-replay rule). Stale decisions are rejected with a conflict."""
    current = await effect_repo.get_effect(effect_id)
    if current is None:
        return {"resolved": False, "error": "NOT_FOUND"}
    if resolution is EffectState.CONFIRMED_NOT_APPLIED and not evidence_references:
        return {"resolved": False, "error":
                "CONFIRMED_NOT_APPLIED requires evidence references (§5.12: "
                "an operator guess must not enable automatic replay)"}
    if resolution is EffectState.CONFIRMED_APPLIED and not evidence_references:
        return {"resolved": False, "error":
                "CONFIRMED_APPLIED requires evidence references"}
    try:
        updated = await effect_repo.transition(
            effect_id, resolution, expected_version=expected_version,
            actor=actor, reason=reason)
        METRICS["resolutions"] = METRICS.get("resolutions", 0) + 1
        return {"resolved": True, "effect_id": effect_id,
                "state": updated.state.value,
                "state_version": updated.state_version,
                "evidence_references": evidence_references}
    except EffectTransitionError as exc:
        return {"resolved": False, "error": "CONFLICT",
                "detail": str(exc), "current_state": exc.current_state.value}


async def observe_and_reconcile_all(effect_repo, limit: int = 25) -> Dict[str, int]:
    """§5.18: the effect observer's bounded pass — run over unresolved
    effects. Strategies are operation-specific; operations without one
    remain unknown (never guessed)."""
    effects = await effect_repo.unresolved_effects(limit)
    counts: Dict[str, int] = {"observed": len(effects), "resolved": 0}
    for effect in effects:
        record = await reconcile_effect(effect_repo, effect.effect_id)
        if record.get("resolved"):
            counts["resolved"] += 1
    return counts
