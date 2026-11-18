"""Routing recorder (Phase 14) — the wiring between selection, the
decision log, and delayed verified feedback.

§14.1: the immediate run-time signal (payload usable / transport error)
is logged as a decision but the LEARNED policy reward comes from the
delayed VERIFIED outcome joined from durable evaluations:
  - VERIFIED   → adaptive.record(model, True)
  - REJECTED   → adaptive.record(model, False)
  - PARTIAL / UNCERTAIN → tracked in the decision log, no policy update
    (unverified completions never become positive rewards, §12.35)
  - MISSING    → explicit in the log; never dropped.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.models.routing_decision import (
    RewardClass, RoutingDecision, outcome_class_for,
)
from backend.repositories.routing_repository import (
    RoutingDecisionRepository, get_routing_repo, new_decision_id,
)
from backend.services.contextual_policy import context_bucket

logger = logging.getLogger("routing_recorder")


def fallback_mechanism(is_fallback: bool, supplied: Optional[str]) -> str:
    if is_fallback:
        return "ranked_first_fit_fallback"
    return supplied or "ranked_first_fit"

POLICY_VERSION = "ucb1-v1-verified-feedback"


async def record_model_selection(
    *, run_id: str, task_id: Optional[str], eligible_actions: List[str],
    selected_action: str, task_category: str = "workflow",
    capability: str = "", input_size_bytes: int = 0,
    resolved_action: Optional[str] = None,
    fallback_from: Optional[str] = None,
    latency_ms: Optional[float] = None,
    selection_mechanism: Optional[str] = None,
    selection_probability: Optional[float] = None,
    task_input: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Log one selection decision (§14.4 + §13.6): context, eligible set,
    choice, the selection mechanism that ACTUALLY ran, and its honest
    probability. The engine's candidate loop is DETERMINISTIC ranked
    first-fit — labeled as such with probability 1.0, never as uniform
    random. Fallbacks carry `fallback_from` (linking to the original
    decision) and their MEASURED latency when known."""
    repo = get_routing_repo()
    if repo is None or not eligible_actions:
        return None
    try:
        is_fallback = fallback_from is not None
        decision = RoutingDecision(
            decision_id=new_decision_id(),
            run_id=run_id, task_id=task_id,
            task_category=task_category,
            capability=capability,
            input_size_bytes=int(input_size_bytes or 0),
            eligible_actions=list(eligible_actions),
            selected_action=selected_action,
            # §13.6/§14.4: the mechanism that ACTUALLY ran — the caller
            # (contextual policy) supplies it; the fallback lineage
            # overrides; the default is the ranked first-fit.
            selection_mechanism=(fallback_mechanism(is_fallback,
                                                    selection_mechanism)),
            selection_probability=(float(selection_probability)
                                   if selection_probability is not None
                                   else 1.0),
            latency_ms=latency_ms,
            policy_version=POLICY_VERSION,
            resolved_action=resolved_action,
            fallback_from=fallback_from,
            reward_class=RewardClass.MISSING.value,
        )
        await repo.log_decision(decision)
        return decision.decision_id
    except Exception:
        logger.debug("Routing decision logging failed", exc_info=True)
        return None


async def attach_evaluation_feedback(
    run_id: str, evaluation: Dict[str, Any],
    evaluation_id: Optional[str] = None, *,
    adaptive=None,
) -> Dict[str, Any]:
    """Join the durable evaluation's verified outcome to the run's
    decisions (exactly once per decision) and update the learned policy
    ONLY from verified/rejected classes."""
    repo = get_routing_repo()
    if repo is None:
        return {"joined": 0}
    reward_class = outcome_class_for(evaluation)
    verified = (reward_class == RewardClass.VERIFIED.value)
    try:
        joined = await repo.attach_feedback(
            run_id, reward_class,
            verified_success=verified if reward_class in (
                RewardClass.VERIFIED.value, RewardClass.REJECTED.value) else None,
            evaluation_id=evaluation_id)
    except Exception:
        logger.debug("Feedback join failed for %s", run_id, exc_info=True)
        return {"joined": 0}
    # §14.18: the reward is published as DURABLE REWARD EVENTS with the
    # unique identity (decision_id, evaluation_id, reward_version) —
    # independent learners (global leaderboard, contextual bandit) consume
    # them through ATOMIC transactions (§14.18/C: the stats update and the
    # consumption mark commit together; a crash neither loses nor
    # duplicates learning, and a retried join cannot create a duplicate
    # event). Fallback lineage is respected: the failed initial model's
    # decision was already rewarded at fallback time (transport failure =
    # verified signal); the reward goes to the row that RESOLVED
    # (resolved_action), never to both — a failed initial model NEVER
    # receives positive credit from the fallback's success.
    applied = 0
    reward_event_ids: List[str] = []
    if reward_class in (
            RewardClass.VERIFIED.value, RewardClass.REJECTED.value):
        verified = reward_class == RewardClass.VERIFIED.value
        try:
            rows = [d for d in await repo.decisions_for_run(run_id)
                    if d.get("feedback_at") is not None
                    and not d.get("policy_applied")]
            fallback_rows = [d for d in rows if d.get("fallback_from")]
            target_rows = fallback_rows or rows
            atomic = (adaptive is not None
                      and hasattr(adaptive, "consume_reward_event")
                      and getattr(adaptive, "repo", None) is not None
                      and hasattr(adaptive.repo, "consume_reward_event"))
            for d in target_rows:
                action = d.get("resolved_action") or d.get("selected_action")
                latency = (d.get("latency_ms")
                           if d.get("latency_ms") is not None else 0.0)
                event_id = None
                try:
                    event_id = await repo.create_reward_event(
                        decision_id=d["decision_id"],
                        evaluation_id=evaluation_id or f"eval:{run_id}",
                        reward_class=reward_class,
                        reward_value=1.0 if verified else 0.0,
                        model=action or "",
                        context_key=context_bucket(
                            d.get("task_category") or "workflow",
                            d.get("capability") or "",
                            int(d.get("input_size_bytes") or 0)),
                        latency_ms=d.get("latency_ms"),
                        cost_units=(float(d["budget_units"])
                                    if d.get("budget_units") is not None
                                    else None))
                except Exception:
                    logger.debug("Reward event creation failed for %s",
                                 d.get("decision_id"), exc_info=True)
                if event_id:
                    reward_event_ids.append(event_id)
                if atomic and event_id:
                    # The learner consumes ATOMICALLY; a False return means
                    # another consumer already applied this event.
                    try:
                        await adaptive.consume_reward_event(event_id)
                    except Exception:
                        logger.debug("Atomic reward consumption failed",
                                     exc_info=True)
                elif adaptive is not None:
                    # Learner without durable-event support: the legacy
                    # two-step (record + marker) path, still idempotent
                    # via the policy_applied marker.
                    await adaptive.record(action, verified, latency)
                await repo.mark_policy_applied(d["decision_id"])
                applied += 1
        except Exception:
            logger.debug("Verified policy update failed for %s", run_id,
                         exc_info=True)
    return {"joined": joined, "reward_class": reward_class,
            "policy_applied": applied,
            "reward_events": reward_event_ids}
