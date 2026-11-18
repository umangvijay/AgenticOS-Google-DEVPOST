"""Browser effect integration (Phase 8 §8.14) — browser actions through the
Phase 5 effect ledger.

A browser click does not provide exactly-once execution. For consequential
actions: record the intended effect → bind to the authorized action →
record dispatch → observe → capture receipts → reconcile uncertainty before
retrying. A browser crash after pressing "Send" leaves OUTCOME_UNKNOWN —
never FAILED→RETRY-SEND (§8.14). Where a site has no reliable
reconciliation mechanism, that limitation is visible and human review is
required.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.models.effect import EffectOutcome, EffectState
from backend.services.browser_runtime import ActionIntent

logger = logging.getLogger(__name__)


async def record_browser_action_effect(
    effect_repo, *, intent: ActionIntent, owner_user_id: str,
    run_id: Optional[str] = None, task_id: Optional[str] = None,
    command_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """§8.14 steps 1-3: record the intended external effect bound to the
    authorized action intent, before the click executes. Only consequential
    intents create effects (observational browsing does not)."""
    if effect_repo is None:
        logger.warning("Effect ledger unavailable; consequential browser "
                       "action refused (§8.14 requires durable effects)")
        return None
    from backend.repositories.effect_repository import request_digest
    # 9.0D: business-action identity = the MATERIAL digest (stable across
    # ownership changes/recovery); the effect_id is DERIVED from it so the
    # same business action maps to the same effect regardless of epoch.
    digest = intent.material_digest()
    from backend.models.effect import Effect
    effect_id = ("fx_ba_" + digest[:28])
    effect = Effect(
        effect_id=effect_id,
        operation_name="browser.action",
        owner_user_id=owner_user_id,
        run_id=run_id, task_id=task_id, command_id=command_id,
        operation_instance_id=intent.action_id,
        target=intent.target_identity,
        canonical_arguments={
            "session_id": intent.session_id,
            "controller_epoch": intent.controller_epoch,
            "frame_origin": intent.frame_origin,
            "action_kind": intent.action_kind.value,
            "target": intent.target_identity,
            "material_payload": intent.material_payload,
        },
        request_digest=digest,
        approval_reference=intent.action_id,
    )
    stored, created = await effect_repo.prepare_effect(effect)
    if not created:
        # §8.14: the same material action was already recorded — a restart
        # must not silently repeat a potentially completed external action.
        logger.warning("Browser effect %s already recorded (state %s); "
                       "reconcile before re-dispatch", stored.effect_id,
                       stored.state.value)
        return {"effect_id": stored.effect_id, "created": False,
                "state": stored.state.value,
                "reconciliation_required": stored.state in (
                    EffectState.DISPATCHING, EffectState.OUTCOME_UNKNOWN)}
    return {"effect_id": stored.effect_id, "created": True,
            "state": stored.state.value, "digest": digest}


async def record_browser_effect_outcome(
    effect_repo, effect_id: str, *, outcome: str,
    evidence_reference: Optional[str] = None,
) -> None:
    """§8.14/9.0D steps 4-6: observe the result. DISPATCHING was already
    transitioned durably BEFORE the browser action (record_browser_dispatch);
    this function records only the observed OUTCOME — uncertainty is
    preserved, never auto-retried."""
    try:
        if outcome == "unknown":
            await effect_repo.transition(
                effect_id, EffectState.OUTCOME_UNKNOWN, actor="browser_runtime",
                reason="browser ended before the result could be confirmed")
        elif outcome == "confirmed":
            await effect_repo.transition(
                effect_id, EffectState.CONFIRMED_APPLIED, actor="browser_runtime",
                reason="browser evidence confirms the action",
                result_reference=evidence_reference)
        # Other outcomes are reconciled through the Phase 5 reconciliation
        # service — never blindly retried.
    except Exception as exc:
        logger.warning("Browser effect outcome recording failed for %s: %s",
                       effect_id, exc)


async def record_browser_dispatch(effect_repo, effect_id: str) -> None:
    """9.0D: durably transition the effect to DISPATCHING BEFORE the browser
    action executes (§8.14 — the external effect may occur)."""
    try:
        await effect_repo.transition(
            effect_id, EffectState.DISPATCHING, actor="browser_runtime",
            reason="browser action dispatch intent (pre-execution)")
    except Exception as exc:
        logger.warning("Browser dispatch recording failed for %s: %s",
                       effect_id, exc)
