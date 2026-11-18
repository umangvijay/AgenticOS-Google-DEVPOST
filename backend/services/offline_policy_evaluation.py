"""Offline policy evaluation (Phase 14 §14.5/§14.6).

Compare candidate routing policies against the LOGGING policy using the
decision log — coverage-aware, with uncertainty reported where the
candidate differs substantially from what was actually run:

- Per-action VERIFIED success by task category (only feedback joined
  from durable evaluations counts — §14.1).
- Coverage check: an action with too few verified samples yields
  "insufficient_support" rather than a confident ranking (§14.5).
- Reward weighting: verified=1, partial=0.5, uncertain=0, rejected=0 —
  an unverified completion NEVER counts as positive reward (§12.35).
- The candidate evaluator is a pure function over (context, eligible
  actions) → chosen action, so any policy (UCB coefficients, category
  rules, fixed baselines) can be replayed against the same log.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("offline_policy_evaluation")

REWARD_WEIGHTS = {
    "verified": 1.0,
    "partial": 0.5,
    "uncertain": 0.0,
    "rejected": 0.0,
    "missing": 0.0,
}

MIN_SUPPORT_PER_ARM = 10   # §14.5: adequate action support
# §14.22: logged propensities below this floor are clipped (the IPS
# weight 1/p would otherwise explode on near-deterministic entries).
PROPENSITY_FLOOR = 0.05


def policy_value(decisions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The LOGGING policy's realized value from verified feedback:
    per-action and per-category weighted reward with sample counts."""
    by_action: Dict[str, Dict[str, Any]] = {}
    by_category: Dict[str, Dict[str, Any]] = {}
    total_weight = 0.0
    counted = 0
    for d in decisions:
        cls = d.get("reward_class") or "missing"
        if cls == "missing":
            continue
        weight = REWARD_WEIGHTS.get(cls, 0.0)
        action = d.get("resolved_action") or d.get("selected_action") or "?"
        category = d.get("task_category") or "workflow"
        for bucket, key in ((by_action, action), (by_category, category)):
            b = bucket.setdefault(key, {"samples": 0, "weight": 0.0,
                                        "verified": 0, "uncertain": 0,
                                        "rejected": 0, "partial": 0})
            b["samples"] += 1
            b["weight"] += weight
            b[cls] = b.get(cls, 0) + 1
        total_weight += weight
        counted += 1
    out = {
        "samples": counted,
        "value": round(total_weight / counted, 4) if counted else None,
        "by_action": {k: _finalize(v) for k, v in by_action.items()},
        "by_category": {k: _finalize(v) for k, v in by_category.items()},
    }
    return out


def _finalize(bucket: Dict[str, Any]) -> Dict[str, Any]:
    samples = bucket["samples"]
    out = dict(bucket)
    out["weight"] = round(bucket["weight"], 4)
    out["value"] = round(bucket["weight"] / samples, 4) if samples else None
    # §14.5: uncertainty — the normal-approximation interval on the mean.
    if samples:
        p = out["value"]
        out["ci95_low"] = round(max(0.0, p - 1.96 * math.sqrt(p * (1 - p) / samples)), 4)
        out["ci95_high"] = round(min(1.0, p + 1.96 * math.sqrt(p * (1 - p) / samples)), 4)
        out["sufficient_support"] = samples >= MIN_SUPPORT_PER_ARM
    else:
        out["ci95_low"] = out["ci95_high"] = None
        out["sufficient_support"] = False
    return out


def replay_candidate(decisions: List[Dict[str, Any]],
                     candidate: Callable[[Dict[str, Any], List[str]], str],
                     *, min_support: int = MIN_SUPPORT_PER_ARM) -> Dict[str, Any]:
    """Replay a candidate policy over the log with TWO clearly separated
    estimators (§14.21/§14.22):

    - `ips` — the SUPPORTED estimator: inverse-propensity scoring over
      the logged selection probabilities, on the agreement set (the
      decisions where the candidate picks the action that was actually
      taken). Where the log is deterministic (every logged propensity
      1.0), `deterministic_log` is flagged: the number is then
      descriptive of the agreement set only and supports NO
      counterfactual claim — stochastic exploration with logged
      propensities (§14.4) is what makes offline evaluation valid.

    - `descriptive` — what the candidate WOULD pick per decision, scored
      by each action's logged verified performance in the same task
      category. Explicitly descriptive (not a policy value estimate);
      picks without adequate support are counted, never silently
      averaged (§14.5).
    """
    # Build action+category reward history from the log.
    history: Dict[tuple, Dict[str, Any]] = defaultdict(
        lambda: {"samples": 0, "weight": 0.0})
    for d in decisions:
        cls = d.get("reward_class") or "missing"
        if cls == "missing":
            continue
        action = d.get("resolved_action") or d.get("selected_action") or "?"
        key = (action, d.get("task_category") or "workflow")
        history[key]["samples"] += 1
        history[key]["weight"] += REWARD_WEIGHTS.get(cls, 0.0)

    estimated = 0.0
    counted = 0
    unsupported = 0
    per_action: Dict[str, Dict[str, int]] = defaultdict(lambda: {"picked": 0})
    ips_num = 0.0
    ips_scored = 0
    ips_agree = 0
    propensity_clipped = 0
    deterministic_entries = 0
    scored_entries = 0
    for d in decisions:
        eligible = d.get("eligible_actions") or [d.get("selected_action")]
        if not eligible:
            continue
        category = d.get("task_category") or "workflow"
        picked = candidate({"task_category": category,
                            "capability": d.get("capability"),
                            "input_size_bytes": d.get("input_size_bytes")},
                           list(eligible))
        if picked not in eligible:
            # A candidate may never select outside the eligible set (§14.3).
            continue
        per_action[picked]["picked"] += 1
        # ── §14.22: the IPS agreement term over logged propensities ──
        cls = d.get("reward_class") or "missing"
        if cls != "missing":
            scored_entries += 1
            p = float(d.get("selection_probability") or 1.0)
            if p >= 1.0:
                deterministic_entries += 1
            if p < PROPENSITY_FLOOR:
                p = PROPENSITY_FLOOR
                propensity_clipped += 1
            taken = d.get("resolved_action") or d.get("selected_action")
            if picked == taken:
                ips_num += REWARD_WEIGHTS.get(cls, 0.0) / p
                ips_agree += 1
            ips_scored += 1
        # ── §14.21: the descriptive replay (support-gated) ────────────
        stats = history.get((picked, category))
        if not stats or stats["samples"] < min_support:
            unsupported += 1
            continue
        estimated += stats["weight"] / stats["samples"]
        counted += 1
    deterministic_log = (scored_entries > 0
                         and deterministic_entries == scored_entries)
    ips_value = (ips_num / ips_scored) if ips_scored else None
    # §14.24: the uncertainty interval on the IPS estimate — a normal
    # approximation over the agreement set's per-decision contributions
    # (r_i/p_i). Reported with the estimate; support gates it.
    ips_ci_low = ips_ci_high = None
    if ips_value is not None and ips_agree > 1:
        import math as _math
        contributions = []
        for d in decisions:
            cls = d.get("reward_class") or "missing"
            if cls == "missing":
                continue
            p = float(d.get("selection_probability") or 1.0)
            if p < PROPENSITY_FLOOR:
                p = PROPENSITY_FLOOR
            taken = d.get("resolved_action") or d.get("selected_action")
            eligible = d.get("eligible_actions") or [taken]
            picked = candidate(
                {"task_category": d.get("task_category") or "workflow",
                 "capability": d.get("capability"),
                 "input_size_bytes": d.get("input_size_bytes")},
                list(eligible))
            if picked == taken:
                contributions.append(REWARD_WEIGHTS.get(cls, 0.0) / p)
        if contributions:
            mean = sum(contributions) / len(contributions)
            var = (sum((x - mean) ** 2 for x in contributions)
                   / (len(contributions) - 1))
            half = 1.96 * _math.sqrt(max(0.0, var) / len(contributions))
            ips_ci_low = round(max(0.0, mean - half), 4)
            ips_ci_high = round(mean + half, 4)
    return {
        # §14.22: the supported estimator + its validity conditions.
        "ips_value": (round(ips_value, 4)
                      if ips_value is not None else None),
        "ips_ci95_low": ips_ci_low,
        "ips_ci95_high": ips_ci_high,
        "ips_sufficient_support": (ips_scored >= MIN_SUPPORT_PER_ARM
                                   and not deterministic_log),
        "ips_agreement_decisions": ips_agree,
        "ips_scored_decisions": ips_scored,
        "propensity_clipped": propensity_clipped,
        "deterministic_log": deterministic_log,
        "counterfactual_supported": (not deterministic_log
                                     and ips_scored >= MIN_SUPPORT_PER_ARM),
        # §14.21: the descriptive replay — explicitly NOT a policy value.
        "descriptive": {
            "estimated_value": round(estimated / counted, 4)
            if counted else None,
            "supported_estimates": counted,
            "unsupported_picks": unsupported,
            "per_action_picked": dict(per_action),
            "note": ("descriptive replay of logged per-action performance; "
                     "not a counterfactual policy value estimate"),
        },
        "replayed": len([d for d in decisions
                         if (d.get("eligible_actions") or
                             [d.get("selected_action")])]),
        "confidence": ("low" if unsupported > (counted or 0) else
                       "medium" if unsupported else "high"),
    }


def compare(decisions: List[Dict[str, Any]],
            candidate: Callable[[Dict[str, Any], List[str]], str]) -> Dict[str, Any]:
    """§14 acceptance: compare candidate routing against the fixed
    logging policy with verified quality + uncertainty by category.
    §14.22: the result names its estimator and what each number is
    allowed to claim — descriptive replay vs propensity-supported IPS."""
    candidate_out = replay_candidate(decisions, candidate)
    return {
        "logging_policy": policy_value(decisions),
        "candidate_policy": candidate_out,
        "estimator": {
            "kind": "ips_agreement+descriptive_replay",
            "counterfactual_supported":
                candidate_out.get("counterfactual_supported", False),
            "requires_for_causal_claims":
                "stochastic exploration with logged propensities (§14.4)",
        },
        "log_health": {
            "decisions": len(decisions),
            "missing_feedback": sum(1 for d in decisions
                                    if (d.get("reward_class") or "missing") == "missing"),
        },
    }


def fixed_policy(action: str) -> Callable[[Dict[str, Any], List[str]], str]:
    """A fixed-baseline candidate (§14 acceptance: 'measurable benefit
    over a fixed baseline')."""
    def _pick(context: Dict[str, Any], eligible: List[str]) -> str:
        return action if action in eligible else (eligible[0] if eligible else action)
    return _pick


def evaluation_hold_reason(evaluation: Dict[str, Any]) -> Optional[str]:
    """30.30: promotion HOLD evaluation (server-side, decisive).

    A favorable training curve or average cannot authorize runtime use
    when the counterfactual evidence does not support it. Returns the
    hold reason when the evaluation record shows:

      - a deterministic log (no overlap): descriptive agreement only,
      - insufficient subgroup/arm support for the picked actions,
      - or an explicit predeclared hold marker.
    """
    if not isinstance(evaluation, dict):
        return None
    if evaluation.get("hold") and evaluation.get("hold_reason"):
        return str(evaluation["hold_reason"])
    unsupported = evaluation.get("descriptive", {}).get("unsupported_picks")
    if unsupported:
        # unsupported_picks is a COUNT of picks without adequate support.
        return ("insufficient subgroup support — "
                f"{unsupported} picked action(s) below the support floor")
    if evaluation.get("deterministic_log"):
        return ("deterministic log with no behavioral overlap — descriptive "
                "agreement only, not a counterfactual value estimate")
    if evaluation.get("counterfactual_supported") is False:
        return ("logged propensities lack adequate coverage for a "
                "counterfactual estimate")
    return None
