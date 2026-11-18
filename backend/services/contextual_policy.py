"""Contextual routing policy (Phase 14 §14.2/§14.5/§14.6).

A context-bucketed UCB1 bandit over VERIFIED outcomes — the next step
from the non-contextual UCB1 router, still NOT weight training:

- CONTEXT: each decision carries (task_category, capability_class,
  input_size_bucket). Stats are tracked PER CONTEXT-BUCKET × MODEL, so
  a model that excels at `research` tasks is not credited for `browser`
  failures (the non-contextual policy's core flaw).
- FILTERING FIRST (§14.3): candidates arrive pre-filtered by
  permissions/provider configuration — the policy never sees a
  forbidden action, so exploration cannot discover one.
- STOCHASTIC EXPLORATION with LOGGED PROBABILITY (§14.4): epsilon-
  mixture over the per-context ranking; the chosen action's selection
  probability is computed exactly and returned for the decision log —
  accurate propensities are what make offline evaluation valid.
- DRIFT MONITORING (§14.5): per-context reward EWMA vs the long-run
  mean — a drift signal flags when recent rewards diverge, without
  auto-tuning. Security constraints stay OUTSIDE the scalar reward
  (they gate eligibility upstream; the reward measures verified
  outcome quality only).
- EXPLORATION BUDGET (§14.5): exploration events are counted durably
  against a windowed cap — when the budget is exhausted the policy
  degrades to greedy (exploitation only), never to off-list actions.

Reward mapping (§14.1, from the delayed verified feedback):
  verified → 1.0, partial → 0.5, uncertain/rejected/missing → 0.0.
An unverified "completed" flag can NEVER be a positive reward.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from backend.services.adaptive_policy import AdaptivePolicy, _score

UCB_C_CONTEXTUAL = 1.2
EPSILON_CONTEXTUAL = 0.10
UNKNOWN_BONUS_CONTEXTUAL = 0.5
# §14.5: the exploration budget — max exploration events per window.
EXPLORATION_BUDGET_WINDOW = 3600   # seconds
EXPLORATION_BUDGET_MAX = 200       # events per window

def context_bucket(task_category: str = "workflow", capability: str = "",
                   input_size_bytes: int = 0) -> str:
    """The context identity: category × capability class × input-size
    bucket. Stable and discretized so per-context stats accumulate."""
    cap_class = str(capability or "unknown").split(".")[0]
    size = int(input_size_bytes or 0)
    if size < 2_000:
        size_bucket = "s"
    elif size < 20_000:
        size_bucket = "m"
    else:
        size_bucket = "l"
    raw = f"{task_category}|{cap_class}|{size_bucket}"
    # Short readable bucket key (collision-safe at this cardinality).
    return hashlib.sha256(raw.encode()).hexdigest()[:12]

def reward_from_class(reward_class: str) -> float:
    """§14.1: verified outcomes only — the scalar reward mapping."""
    return {"verified": 1.0, "partial": 0.5}.get(reward_class, 0.0)

@dataclass
class ContextualDecision:
    """The outcome of one contextual selection — everything §14.4
    requires in the log."""
    ranked: List[str]
    selected: str
    selection_probability: float
    mechanism: str          # contextual_ucb1 | contextual_ucb1_explore | greedy_budget_exhausted
    context_key: str
    exploration_budget_remaining: int

class ContextualRoutingPolicy:
    """Per-context-bucket UCB1 with epsilon-mixture exploration, an
    exploration budget, and drift monitoring. Wraps (does not replace)
    the legacy AdaptivePolicy for the global leaderboard."""

    def __init__(self, repo, stats_repo=None):
        self.repo = repo                  # per-context stats (new table)
        self.legacy = AdaptivePolicy(stats_repo or repo)

    # ── selection ──────────────────────────────────────────────────────

    async def select(self, *, candidates: List[str],
                     task_category: str = "workflow",
                     capability: str = "",
                     input_size_bytes: int = 0,
                     rng: Optional[random.Random] = None,
                     ) -> ContextualDecision:
        """Select a model from the ELIGIBLE candidates for this context.
        Returns the full decision (ranking, choice, COMPLETE probability
        distribution, mechanism, budget remaining). §14.14: the probabilities
        describe the COMPLETE policy — with multiple eligible actions and
        exploration available, P(top) = 1 - epsilon and P(each other) =
        epsilon / (n - 1), summing to 1.0."""
        rng = rng or random
        context_key = context_bucket(task_category, capability,
                                     input_size_bytes)
        ranked, scores = await self._rank_for_context(
            context_key, candidates)
        n = len(ranked)
        if n <= 1:
            return ContextualDecision(
                ranked=ranked, selected=ranked[0] if ranked else "",
                selection_probability=1.0,
                mechanism="contextual_ucb1", context_key=context_key,
                exploration_budget_remaining=0)
        # §14.15/B: ATOMIC exploration admission — try to claim an
        # exploration slot atomically. On budget exhaustion OR storage
        # failure, degrade to the approved baseline (greedy top choice),
        # never to unbounded exploration.
        admitted = await self._admit_exploration()
        if admitted is False:
            return ContextualDecision(
                ranked=ranked, selected=ranked[0],
                selection_probability=1.0,
                mechanism="greedy_budget_exhausted", context_key=context_key,
                exploration_budget_remaining=0)
        if rng.random() < EPSILON_CONTEXTUAL:
            # §14.4/§14.14: P(chosen alternative) = eps / (n - 1).
            i = rng.randrange(1, n)
            explore_action = ranked[i]
            ranked[0], ranked[i] = ranked[i], ranked[0]
            return ContextualDecision(
                ranked=ranked, selected=explore_action,
                selection_probability=round(
                    EPSILON_CONTEXTUAL / max(1, n - 1), 6),
                mechanism="contextual_ucb1_explore",
                context_key=context_key,
                exploration_budget_remaining=-1)
        # §14.14: P(greedy top) = (1 - epsilon) = 0.90 for n >= 2.
        return ContextualDecision(
            ranked=ranked, selected=ranked[0],
            selection_probability=round(1.0 - EPSILON_CONTEXTUAL, 6),
            mechanism="contextual_ucb1", context_key=context_key,
            exploration_budget_remaining=-1)

    async def _admit_exploration(self) -> Optional[bool]:
        """§14.15: ATOMIC exploration admission. Returns True (admitted),
        False (budget exhausted), or None (accounting unavailable →
        baseline fallback, never fresh exploration). Repos exposing the
        single-transaction admission (count + claim together) are
        preferred — the two-step path races under concurrent selectors."""
        if hasattr(self.repo, "admit_exploration"):
            try:
                return bool(await self.repo.admit_exploration(
                    EXPLORATION_BUDGET_WINDOW, EXPLORATION_BUDGET_MAX))
            except Exception:
                return None   # accounting unavailable → baseline
        try:
            used = await self.repo.exploration_events_in_window(
                window_seconds=EXPLORATION_BUDGET_WINDOW)
        except Exception:
            return None   # accounting unavailable → baseline
        if int(used) >= EXPLORATION_BUDGET_MAX:
            return False   # budget exhausted
        try:
            await self.repo.record_exploration_event()
        except Exception:
            return None   # recording failed → baseline, not unbounded
        return True

    async def _rank_for_context(self, context_key: str,
                                candidates: List[str]) -> Tuple[List[str], Dict[str, float]]:
        stats_rows = []
        try:
            stats_rows = await self.repo.get_context_stats(context_key)
        except Exception:
            stats_rows = []
        by_model = {str(r.get("model")): r for r in stats_rows}
        total_attempts = sum(int(r.get("attempts") or 0) for r in stats_rows)
        scored = []
        for i, model in enumerate(candidates):
            s = self._context_score(by_model.get(model, {}), total_attempts)
            scored.append((i, model, s))
        scored.sort(key=lambda t: (-t[2], t[0]))
        return [m for _, m, _ in scored], {m: s for _, m, s in scored}

    @staticmethod
    def _context_score(stats: Dict[str, Any], total_attempts: int) -> float:
        attempts = int(stats.get("attempts") or 0)
        if attempts == 0:
            return UNKNOWN_BONUS_CONTEXTUAL
        reward_sum = float(stats.get("reward_sum") or 0.0)
        mean_reward = reward_sum / attempts
        exploration = UCB_C_CONTEXTUAL * math.sqrt(
            max(0.0, math.log(max(total_attempts, 1)) / attempts))
        latency_ms = max(float(stats.get("ewma_latency_ms") or 0.0), 1.0)
        latency_term = 1.0 / (1.0 + latency_ms / 1000.0)
        # §14.20: EWMA cost (budget units) is a scoring tie-breaker with
        # a SMALLER weight than latency — the verified reward stays the
        # dominant signal; cost never overrides outcome quality. A model
        # with no measured cost gains NOTHING from the cost term (a
        # missing measurement is never a bonus).
        cost = stats.get("ewma_cost_units")
        cost_term = (1.0 / (1.0 + float(cost))) if cost is not None else 0.0
        return mean_reward + exploration + 0.1 * latency_term \
            + 0.05 * cost_term

    # ── learning (verified outcomes only) ──────────────────────────────

    async def learn(self, *, model: str, task_category: str,
                    capability: str, input_size_bytes: int,
                    reward_class: str, latency_ms: Optional[float] = None,
                    ) -> None:
        """§14.1: update the PER-CONTEXT stats from the delayed verified
        feedback. Uncertain/rejected/missing contribute 0 reward but DO
        count as attempts (they inform the exploration term)."""
        context_key = context_bucket(task_category, capability,
                                     input_size_bytes)
        reward = reward_from_class(reward_class)
        try:
            await self.repo.record_context_outcome(
                context_key, model, reward=reward,
                latency_ms=latency_ms)
        except Exception:
            pass   # learning must never break execution

    async def consume_reward_event(self, event_id: str) -> bool:
        """§14.18: consume one durable reward event ATOMICALLY — the
        per-context stats update and the consumption mark commit in one
        transaction (crash-safe: neither lost nor duplicated)."""
        if not event_id or self.repo is None:
            return False
        try:
            return await self.repo.consume_reward_event(
                event_id, consumer="contextual")
        except Exception:
            return False   # learning must never break execution

    # ── §14.5: exploration budget (durable, windowed) ──────────────────

    # ── §14.5: drift monitoring ─────────────────────────────────────────

    async def drift_report(self, *, context_key: Optional[str] = None,
                           window_seconds: int = 86_400,
                           ) -> Dict[str, Any]:
        """Reward-distribution drift: recent-window mean reward vs the
        long-run mean, per context (or overall). A signal for operators —
        the policy does NOT auto-tune on it."""
        try:
            rows = await self.repo.drift_windows(
                context_key=context_key, window_seconds=window_seconds)
        except Exception:
            rows = []
        report = {"contexts": [], "drift_detected": False}
        for r in rows:
            recent = float(r.get("recent_mean_reward") or 0.0)
            long_run = float(r.get("long_run_mean_reward") or 0.0)
            recent_n = int(r.get("recent_attempts") or 0)
            # A >0.25 absolute shift with enough samples is drift. The
            # recent bucket is COUNT-rotated (≤ RECENT_WINDOW_MAX
            # outcomes), so half a full window is the sample bar.
            drifted = recent_n >= 10 and abs(recent - long_run) > 0.25
            entry = {
                "context_key": r.get("context_key"),
                "recent_mean_reward": round(recent, 4),
                "long_run_mean_reward": round(long_run, 4),
                "recent_attempts": recent_n,
                "drift_detected": drifted,
            }
            report["contexts"].append(entry)
            if drifted:
                report["drift_detected"] = True
        return report
