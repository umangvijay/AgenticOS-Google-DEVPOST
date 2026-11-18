"""
AgentOS — SQLite Adaptive Policy Repository

Persistent memory for the online-learning model-selection policy:
per-model attempt/success counters and EWMA latency, updated from real
task outcomes. See backend/services/adaptive_policy.py for the policy.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Any, Dict, List

from backend.repositories.base import BaseAdaptivePolicyRepository
from backend.repositories.sqlite.database import DatabaseManager

_stats_repo = None


def set_adaptive_policy_repo(repo) -> None:
    global _stats_repo
    _stats_repo = repo


def get_adaptive_policy_repo():
    """The module-level stats repo (wired at factory init) — the
    learned-selection path reads it; None until configured."""
    return _stats_repo

logger = logging.getLogger(__name__)

# §14.29: the recent-window length for drift monitoring — the last N
# outcomes are the "recent" window the drift report compares against
# the long-run mean.
RECENT_WINDOW_MAX = 20


class SQLiteAdaptivePolicyRepository(BaseAdaptivePolicyRepository):

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def _apply_global_outcome(self, conn, model: str, *,
                                    success: bool, latency_ms: float) -> None:
        """The global learner's stats update against an OPEN transaction
        (no commit — the caller owns the transaction boundary)."""
        if not model:
            return
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            """
            INSERT INTO model_stats (model, attempts, successes, failures, ewma_latency_ms, updated_at)
            VALUES (?, 1, ?, ?, ?, ?)
            ON CONFLICT(model) DO UPDATE SET
                attempts = attempts + 1,
                successes = successes + ?,
                failures = failures + ?,
                ewma_latency_ms = ewma_latency_ms * 0.7 + ? * 0.3,
                updated_at = ?
            """,
            (
                model,
                1 if success else 0,
                0 if success else 1,
                float(latency_ms),
                now,
                1 if success else 0,
                0 if success else 1,
                float(latency_ms),
                now,
            ),
        )

    async def record_outcome(self, model: str, success: bool, latency_ms: float) -> None:
        conn = await self.db.connection()
        await self._apply_global_outcome(conn, model, success=success,
                                         latency_ms=latency_ms)
        await conn.commit()

    async def get_stats(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM model_stats ORDER BY attempts DESC")
        return [dict(r) for r in rows]

    # ── Phase 14: contextual bandit stats (per context-bucket × model) ──

    async def _apply_context_outcome(self, conn, context_key: str,
                                     model: str, *, reward: float,
                                     latency_ms: Optional[float] = None,
                                     cost_units: Optional[float] = None) -> None:
        """The contextual learner's stats update against an OPEN
        transaction (no commit — the caller owns the boundary)."""
        now_ts = int(datetime.now(timezone.utc).timestamp())
        now = datetime.now(timezone.utc).isoformat()
        cursor = await conn.execute(
            "SELECT attempts, reward_sum, ewma_latency_ms, ewma_cost_units, "
            "recent_reward_sum, recent_attempts, recent_window_started_at "
            "FROM contextual_model_stats WHERE context_key=? AND model=?",
            (context_key, model))
        row = await cursor.fetchone()
        if row is None:
            attempts, reward_sum = 0, 0.0
            ewma = latency_ms
            ewma_cost = cost_units
            recent_reward, recent_attempts, window_started = 0.0, 0, now_ts
        else:
            attempts = int(row["attempts"] or 0)
            reward_sum = float(row["reward_sum"] or 0.0)
            ewma = (float(row["ewma_latency_ms"])
                    if row["ewma_latency_ms"] is not None else None)
            ewma_cost = (float(row["ewma_cost_units"])
                         if row["ewma_cost_units"] is not None else None)
            recent_reward = float(row["recent_reward_sum"] or 0.0)
            recent_attempts = int(row["recent_attempts"] or 0)
            window_started = int(row["recent_window_started_at"] or 0)
        attempts += 1
        reward_sum += reward
        # §14.20: the FIRST observation seeds the EWMAs with the measured
        # value itself (0.7/0.3-blending the first sample with itself
        # would double-apply it); later observations blend.
        if row is not None:
            if latency_ms is not None:
                ewma = (ewma * 0.7 + latency_ms * 0.3) if ewma is not None \
                    else latency_ms
            if cost_units is not None:
                ewma_cost = (ewma_cost * 0.7 + cost_units * 0.3) \
                    if ewma_cost is not None else cost_units
        # §14.29: the "recent" bucket holds the LAST RECENT_WINDOW_MAX
        # outcomes (count-rotated), so drift compares a bounded recent
        # window against the long-run mean — a daily rotation would let
        # a full day of healthy traffic mask a recent collapse.
        if recent_attempts >= RECENT_WINDOW_MAX:
            recent_reward, recent_attempts, window_started = reward, 1, now_ts
        else:
            recent_reward += reward
            recent_attempts += 1
        await conn.execute(
            """
            INSERT INTO contextual_model_stats
                (context_key, model, attempts, reward_sum, ewma_latency_ms,
                 ewma_cost_units, recent_reward_sum, recent_attempts,
                 recent_window_started_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(context_key, model) DO UPDATE SET
                attempts=excluded.attempts, reward_sum=excluded.reward_sum,
                ewma_latency_ms=excluded.ewma_latency_ms,
                ewma_cost_units=excluded.ewma_cost_units,
                recent_reward_sum=excluded.recent_reward_sum,
                recent_attempts=excluded.recent_attempts,
                recent_window_started_at=excluded.recent_window_started_at,
                updated_at=excluded.updated_at
            """,
            (context_key, model, attempts, reward_sum, ewma, ewma_cost,
             recent_reward, recent_attempts, window_started, now))

    async def record_context_outcome(self, context_key: str, model: str, *,
                                     reward: float,
                                     latency_ms: Optional[float] = None) -> None:
        conn = await self.db.connection()
        await self._apply_context_outcome(conn, context_key, model,
                                          reward=reward,
                                          latency_ms=latency_ms)
        await conn.commit()

    async def consume_reward_event(self, event_id: str, *,
                                   consumer: str = "contextual",
                                   routing_repo=None) -> bool:
        """§14.18/C: ATOMIC reward consumption — the learner's stats
        update and the consumption mark commit in ONE transaction.

        - Crash BEFORE the commit → neither happened; the event stays
          unconsumed and the retry re-applies (no lost learning).
        - Crash AFTER the commit → both happened; a retry's CAS on
          consumed=0 fails and applies nothing (no double learning).

        `consumer` selects the learner: 'contextual' (per-context
        bandit stats) or 'global' (the legacy leaderboard stats)."""
        if routing_repo is None:
            from backend.repositories.routing_repository import (
                get_routing_repo)
            routing_repo = get_routing_repo()
        col = ("consumed_by_contextual" if consumer == "contextual"
               else "consumed_by_global")
        conn = await self.db.connection()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                "SELECT * FROM reward_events WHERE event_id=?", (event_id,))
            row = await cursor.fetchone()
            if row is None:
                await conn.rollback()
                return False
            ev = dict(row)
            # Already consumed → nothing to apply (idempotent replay).
            if int(ev[col] or 0) == 1:
                await conn.rollback()
                return False
            reward_value = ev.get("reward_value")
            model = ev.get("model")
            # §14.21: malformed events are QUARANTINED — marked consumed
            # with a visible reason so they cannot poison the learner
            # loop or re-process forever, but never applied as learning.
            malformed = (
                model is None or str(model) == ""
                or reward_value is None
                or not isinstance(reward_value, (int, float))
                or float(reward_value) != float(reward_value)
                or not (0.0 <= float(reward_value) <= 1.0))
            if malformed:
                mark = await conn.execute(
                    f"UPDATE reward_events SET {col}=1, quarantined=1, "
                    f"quarantine_reason=? WHERE event_id=? AND {col}=0",
                    ("malformed reward event: invalid model/reward_value",
                     event_id))
                await conn.commit()
                return bool(mark.rowcount)
            if consumer == "contextual":
                await self._apply_context_outcome(
                    conn, str(ev.get("context_key") or ""),
                    str(model),
                    reward=float(reward_value),
                    latency_ms=ev.get("latency_ms"),
                    cost_units=ev.get("cost_units"))
            else:
                await self._apply_global_outcome(
                    conn, str(model),
                    success=float(reward_value) >= 1.0,
                    latency_ms=float(ev.get("latency_ms") or 0.0))
            mark = await conn.execute(
                f"UPDATE reward_events SET {col}=1 WHERE event_id=? AND {col}=0",
                (event_id,))
            applied = bool(mark.rowcount)
            await conn.commit()
            return applied
        except Exception:
            await conn.rollback()
            raise

    async def get_context_stats(self, context_key: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM contextual_model_stats WHERE context_key = ?",
            (context_key,))
        return [dict(r) for r in rows]

    async def all_context_stats(self) -> List[Dict[str, Any]]:
        """§14.25: every context row — the policy artifact's material."""
        rows = await self.db.fetch_all("SELECT * FROM contextual_model_stats")
        return [dict(r) for r in rows]

    async def restore_context_snapshot(self, rows: List[Dict[str, Any]]) -> int:
        """§14.25: replace the contextual stats with a snapshot's values —
        the restore reproduces the policy exactly (rollback, §13.5)."""
        conn = await self.db.connection()
        await conn.execute("DELETE FROM contextual_model_stats")
        restored = 0
        now = datetime.now(timezone.utc).isoformat()
        for r in rows or []:
            if not r.get("context_key") or not r.get("model"):
                continue
            await conn.execute(
                """
                INSERT INTO contextual_model_stats
                    (context_key, model, attempts, reward_sum,
                     ewma_latency_ms, ewma_cost_units, recent_reward_sum,
                     recent_attempts, recent_window_started_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(context_key, model) DO UPDATE SET
                    attempts=excluded.attempts, reward_sum=excluded.reward_sum,
                    ewma_latency_ms=excluded.ewma_latency_ms,
                    ewma_cost_units=excluded.ewma_cost_units,
                    recent_reward_sum=excluded.recent_reward_sum,
                    recent_attempts=excluded.recent_attempts,
                    recent_window_started_at=excluded.recent_window_started_at,
                    updated_at=excluded.updated_at
                """,
                (r["context_key"], r["model"],
                 int(r.get("attempts") or 0), float(r.get("reward_sum") or 0.0),
                 r.get("ewma_latency_ms"), r.get("ewma_cost_units"),
                 float(r.get("recent_reward_sum") or 0.0),
                 int(r.get("recent_attempts") or 0),
                 int(r.get("recent_window_started_at") or 0), now))
            restored += 1
        await conn.commit()
        return restored

    async def exploration_events_in_window(self, window_seconds: int) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM exploration_events WHERE created_at >= ?",
            (int(datetime.now(timezone.utc).timestamp()) - int(window_seconds),))
        return int(row["n"]) if row else 0

    async def record_exploration_event(self) -> None:
        conn = await self.db.connection()
        await conn.execute(
            "INSERT INTO exploration_events (created_at) VALUES (?)",
            (int(datetime.now(timezone.utc).timestamp()),))
        await conn.commit()

    async def admit_exploration(self, window_seconds: int,
                                max_events: int) -> bool:
        """§14.15: ATOMIC budget admission — the windowed count and the
        event creation commit in ONE transaction, so concurrent
        selectors cannot collectively exceed the cap (the two-step
        count-then-insert path races under load)."""
        conn = await self.db.connection()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cutoff = (int(datetime.now(timezone.utc).timestamp())
                      - int(window_seconds))
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM exploration_events "
                "WHERE created_at >= ?", (cutoff,))
            row = await cur.fetchone()
            if int(row["n"]) >= int(max_events):
                await conn.rollback()
                return False
            await conn.execute(
                "INSERT INTO exploration_events (created_at) VALUES (?)",
                (int(datetime.now(timezone.utc).timestamp()),))
            await conn.commit()
            return True
        except Exception:
            await conn.rollback()
            raise

    async def drift_windows(self, *, context_key: Optional[str] = None,
                            window_seconds: int = 86_400) -> List[Dict[str, Any]]:
        """§14.5: recent-window vs long-run mean reward per context."""
        cutoff = int(datetime.now(timezone.utc).timestamp()) - int(window_seconds)
        sql = (
            "SELECT context_key, "
            "SUM(CASE WHEN recent_window_started_at >= ? THEN recent_reward_sum ELSE 0 END) AS recent_reward_sum, "
            "SUM(CASE WHEN recent_window_started_at >= ? THEN recent_attempts ELSE 0 END) AS recent_attempts, "
            "SUM(reward_sum) AS total_reward_sum, SUM(attempts) AS total_attempts "
            "FROM contextual_model_stats")
        params: List[Any] = [cutoff, cutoff]
        if context_key:
            sql += " WHERE context_key = ?"
            params.append(context_key)
        sql += " GROUP BY context_key"
        rows = await self.db.fetch_all(sql, tuple(params))
        out = []
        for r in rows or []:
            d = dict(r)
            recent_n = int(d.get("recent_attempts") or 0)
            total_n = int(d.get("total_attempts") or 0)
            d["recent_mean_reward"] = (
                float(d["recent_reward_sum"]) / recent_n if recent_n else 0.0)
            d["long_run_mean_reward"] = (
                float(d["total_reward_sum"]) / total_n if total_n else 0.0)
            out.append(d)
        return out

    async def restore_snapshot(self, stats: List[Dict[str, Any]]) -> int:
        """Phase 13 §13.5: versioned policy rollback — replace the live
        counters with a durable snapshot's values (the learner never
        authorizes anything; this only restores learned statistics)."""
        conn = await self.db.connection()
        await conn.execute("DELETE FROM model_stats")
        restored = 0
        for row in stats or []:
            model = row.get("model")
            if not model:
                continue
            await conn.execute(
                """
                INSERT INTO model_stats (model, attempts, successes, failures,
                                         ewma_latency_ms, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (model, int(row.get("attempts", 0)),
                 int(row.get("successes", 0)), int(row.get("failures", 0)),
                 float(row.get("ewma_latency_ms", 0.0)),
                 datetime.now(timezone.utc).isoformat()))
            restored += 1
        await conn.commit()
        return restored

    async def reset(self) -> int:
        conn = await self.db.connection()
        cursor = await conn.execute("DELETE FROM model_stats")
        await conn.commit()
        return cursor.rowcount or 0
