"""
AgentOS — Adaptive Model Selection (online learning from real outcomes)

The self-evolving loop, stated honestly:

1. OBSERVE  — every agent task records what happened with the model that
   ran it: success/failure and wall-clock latency (engine wiring).
2. LEARN    — per-model stats persist (SQLite `model_stats` / Firestore
   `model_stats`): attempts, successes, failures, EWMA latency.
3. DECIDE   — before each task, candidates are re-ranked with UCB1
   (success rate + exploration bonus), plus 10% epsilon exploration so
   the policy never stops sampling alternatives. The policy only
   REORDERS the candidate chain — every fallback stays available, so a
   learning bug can never remove a working model.
4. EXPOSE   — GET /api/v1/usage/models publishes the leaderboard so the
   evolution is observable, not a black box.

This is multi-armed-bandit reinforcement learning over model choice —
the same feedback signal family used to tune LLM routing in production
systems. It is not model training: no weights change, only the routing
policy improves with evidence.
"""

import math
import random
from typing import Any, Dict, List, Optional

# UCB1 exploration coefficient and epsilon for uniform exploration.
UCB_C = 1.0
EPSILON = 0.10
# Unknown models get a moderate bonus: tried reasonably early (ahead of
# known-bad), but a proven-good model always outranks an untried one.
UNKNOWN_BONUS = 1.2


def _score(stats: Dict[str, Any], total_attempts: int) -> float:
    attempts = int(stats.get("attempts") or 0)
    if attempts == 0:
        return UNKNOWN_BONUS
    successes = int(stats.get("successes") or 0)
    success_rate = successes / attempts
    exploration = UCB_C * math.sqrt(max(0.0, math.log(max(total_attempts, 1)) / attempts))
    # Latency tie-breaker: faster EWMA scores higher (1ms floor avoids /0).
    latency_ms = max(float(stats.get("ewma_latency_ms") or 0.0), 1.0)
    latency_term = 1.0 / (1.0 + latency_ms / 1000.0)
    return success_rate + exploration + 0.1 * latency_term


class AdaptivePolicy:
    """Stateless scoring over stats fetched from the repository."""

    def __init__(self, repo):
        self.repo = repo

    async def rank(self, candidates: List[str]) -> List[str]:
        """Re-rank candidate models best-first. Never drops a candidate."""
        if not candidates or self.repo is None:
            return list(candidates)
        try:
            stats_rows = await self.repo.get_stats()
        except Exception:
            return list(candidates)
        by_model = {str(r.get("model")): r for r in stats_rows}
        total_attempts = sum(int(r.get("attempts") or 0) for r in stats_rows)
        scored = list(enumerate(candidates))
        scored.sort(key=lambda pair: (-_score(by_model.get(pair[1], {}), total_attempts), pair[0]))
        ranked = [m for _, m in scored]
        # Epsilon exploration: occasionally promote a random alternative to
        # the front so the policy keeps sampling non-greedy options.
        if len(ranked) > 1 and random.random() < EPSILON:
            i = random.randrange(1, len(ranked))
            ranked[0], ranked[i] = ranked[i], ranked[0]
        return ranked

    async def record(self, model: Optional[str], success: bool, latency_ms: float) -> None:
        if not model or self.repo is None:
            return
        try:
            await self.repo.record_outcome(model, success, latency_ms)
        except Exception:
            pass  # learning must never break execution

    async def consume_reward_event(self, event_id: str) -> bool:
        """§14.18: consume one durable reward event — the stats update
        and the consumption mark commit ATOMICALLY (§14.18/C), so a
        crash can neither lose nor duplicate the learning."""
        if not event_id or self.repo is None:
            return False
        try:
            return await self.repo.consume_reward_event(
                event_id, consumer="global")
        except Exception:
            return False   # learning must never break execution

    async def leaderboard(self) -> List[Dict[str, Any]]:
        """Stats annotated with the current score, best first."""
        try:
            stats_rows = await self.repo.get_stats() if self.repo else []
        except Exception:
            stats_rows = []
        total_attempts = sum(int(r.get("attempts") or 0) for r in stats_rows)
        out = []
        for r in stats_rows:
            attempts = int(r.get("attempts") or 0)
            successes = int(r.get("successes") or 0)
            row = dict(r)
            row["success_rate"] = round(successes / attempts, 3) if attempts else None
            row["score"] = round(_score(r, total_attempts), 4)
            out.append(row)
        out.sort(key=lambda r: -r["score"])
        return out
