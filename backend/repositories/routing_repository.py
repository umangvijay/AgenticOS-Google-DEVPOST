"""Routing decision repository (Phase 14) — the logged bandit data.

Poor logging cannot be repaired by a clever estimator (§14.4), so the
contract is: append-only decision rows with the full context, the
eligible set, the choice + probability, and explicit (nullable) delayed
feedback. SQLite reference + Firestore parity.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now
from backend.models.routing_decision import RewardClass, RoutingDecision


def new_decision_id() -> str:
    return f"rd_{uuid.uuid4().hex[:20]}"


class RoutingDecisionRepository:
    """SQLite reference implementation."""

    def __init__(self, db):
        self.db = db

    async def log_decision(self, decision: RoutingDecision) -> RoutingDecision:
        decision.created_at = decision.created_at or epoch_now()
        row = decision.to_row()
        await self.db.execute(
            """
            INSERT INTO routing_decisions
                (decision_id, run_id, task_id, task_category, capability,
                 input_size_bytes, latency_target_ms, budget_units,
                 eligible_actions_json, selected_action, selection_mechanism,
                 selection_probability, latency_ms, policy_applied,
                 policy_version, resolved_action, fallback_from,
                 reward_class, reward_verified_success, feedback_at,
                 evaluation_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["decision_id"], row["run_id"], row["task_id"],
             row["task_category"], row["capability"],
             row["input_size_bytes"], row["latency_target_ms"],
             row["budget_units"], row["eligible_actions_json"],
             row["selected_action"], row["selection_mechanism"],
             row["selection_probability"], row["latency_ms"],
             row["policy_applied"],
             row["policy_version"], row["resolved_action"],
             row["fallback_from"], row["reward_class"],
             row["reward_verified_success"], row["feedback_at"],
             row["evaluation_id"], row["created_at"]))
        await self.db.commit()
        return decision

    async def attach_feedback(self, run_id: str, reward_class: str,
                              verified_success: Optional[bool],
                              evaluation_id: Optional[str] = None) -> int:
        """§14.1: join the delayed verified outcome to every decision for
        the run that has not received feedback yet. Decisions are updated
        EXACTLY once — redelivered terminal events cannot rewrite them."""
        now = epoch_now()
        cursor = await self.db.execute(
            "UPDATE routing_decisions SET reward_class=?, "
            "reward_verified_success=?, feedback_at=?, evaluation_id=? "
            "WHERE run_id=? AND feedback_at IS NULL",
            (reward_class,
             None if verified_success is None else (1 if verified_success else 0),
             now, evaluation_id, run_id))
        await self.db.commit()
        return cursor.rowcount or 0

    async def create_reward_event(self, *, decision_id: str,
                                  evaluation_id: str,
                                  reward_class: str, reward_value: float,
                                  model: str, context_key: str = "",
                                  latency_ms: Optional[float] = None,
                                  cost_units: Optional[float] = None,
                                  reward_version: str = "v1") -> Optional[str]:
        """§14.18: create an immutable reward event. Idempotent — a
        duplicate (decision, evaluation, version) returns the existing
        event's id without creating a second one."""
        import uuid as _uuid
        event_id = f"rew_{_uuid.uuid4().hex[:20]}"
        now = epoch_now()
        await self.db.execute(
            """INSERT INTO reward_events
               (event_id, decision_id, evaluation_id, reward_version,
                reward_class, reward_value, model, context_key,
                latency_ms, cost_units, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(decision_id, evaluation_id, reward_version)
               DO NOTHING""",
            (event_id, decision_id, evaluation_id, reward_version,
             reward_class, reward_value, model, context_key,
             latency_ms, cost_units, now))
        await self.db.commit()
        # Retrieve the actual event id (may be the pre-existing one).
        cursor = await self.db.execute(
            "SELECT event_id FROM reward_events WHERE decision_id=? AND "
            "evaluation_id=? AND reward_version=?",
            (decision_id, evaluation_id, reward_version))
        row = await cursor.fetchone()
        return row["event_id"] if row else None

    async def unconsumed_reward_events(self, consumer: str,
                                       reward_version: str = "v1",
                                       limit: int = 100) -> List[Dict[str, Any]]:
        """Reward events not yet consumed by the named learner."""
        col = "consumed_by_contextual" if consumer == "contextual" \
              else "consumed_by_global"
        cursor = await self.db.execute(
            f"SELECT * FROM reward_events WHERE reward_version=? AND "
            f"{col}=0 ORDER BY created_at LIMIT ?", (reward_version, limit))
        return [dict(r) for r in await cursor.fetchall()]

    async def mark_reward_consumed(self, event_id: str, consumer: str) -> bool:
        """§14.18: mark a reward event as consumed by the named learner —
        CAS semantics so a concurrent consumer cannot double-apply."""
        col = "consumed_by_contextual" if consumer == "contextual" \
              else "consumed_by_global"
        cursor = await self.db.execute(
            f"UPDATE reward_events SET {col}=1 WHERE event_id=? AND {col}=0",
            (event_id,))
        await self.db.commit()
        return bool(cursor.rowcount)

    async def mark_policy_applied(self, decision_id: str) -> None:
        """§13.6: idempotent policy application — the marker prevents
        double-applying a reward on retry."""
        await self.db.execute(
            "UPDATE routing_decisions SET policy_applied=1 WHERE decision_id=?",
            (decision_id,))
        await self.db.commit()

    async def decisions_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM routing_decisions WHERE run_id=? ORDER BY created_at",
            (run_id,))
        return [_row_to_decision(r) for r in await cursor.fetchall()]

    async def logged_decisions(self, limit: int = 5000) -> List[Dict[str, Any]]:
        """§14.5 offline-evaluation input: recent decisions with feedback
        (feedback_at NOT NULL) plus the missing-feedback count."""
        cursor = await self.db.execute(
            "SELECT * FROM routing_decisions WHERE feedback_at IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?", (limit,))
        return [_row_to_decision(r) for r in await cursor.fetchall()]

    async def summary_by_category(self, *, limit: int = 5000) -> Dict[str, Any]:
        """§14.2: the roadmap acceptance's measured report — per task
        category: verified quality (reward mean + CI95 + support), cost
        (mean budget units of rewarded decisions), latency (mean
        measured ms), and confidence. Decisions WITHOUT feedback are
        counted explicitly as missing — never dropped, never averaged
        in as zero."""
        # ALL recent decisions — including those still missing feedback,
        # which the summary reports EXPLICITLY (never dropped, never
        # averaged in as zero).
        cursor = await self.db.execute(
            "SELECT * FROM routing_decisions ORDER BY created_at DESC "
            "LIMIT ?", (limit,))
        from backend.repositories.routing_repository import _row_to_decision
        rows = [_row_to_decision(r) for r in await cursor.fetchall()]
        cats: Dict[str, Dict[str, Any]] = {}
        for d in rows:
            cat = d.get("task_category") or "workflow"
            c = cats.setdefault(cat, {
                "decisions": 0, "scored": 0, "weight": 0.0,
                "latency_ms_sum": 0.0, "latency_n": 0,
                "cost_units_sum": 0.0, "cost_n": 0,
                "by_class": {}})
            c["decisions"] += 1
            cls = d.get("reward_class") or "missing"
            c["by_class"][cls] = c["by_class"].get(cls, 0) + 1
            if cls == "missing":
                continue
            from backend.services.offline_policy_evaluation import (
                REWARD_WEIGHTS)
            weight = REWARD_WEIGHTS.get(cls, 0.0)
            c["scored"] += 1
            c["weight"] += weight
            if d.get("latency_ms") is not None:
                c["latency_ms_sum"] += float(d["latency_ms"])
                c["latency_n"] += 1
            if d.get("budget_units") is not None:
                c["cost_units_sum"] += float(d["budget_units"])
                c["cost_n"] += 1
        out = {}
        for cat, c in cats.items():
            scored = c["scored"]
            value = (c["weight"] / scored) if scored else None
            entry: Dict[str, Any] = {
                "decisions": c["decisions"],
                "scored": scored,
                "missing_feedback": c["by_class"].get("missing", 0),
                "by_class": c["by_class"],
                "verified_quality": round(value, 4) if value is not None else None,
                "confidence": ("none" if not scored else
                               "low" if scored < 10 else
                               "medium" if scored < 30 else "high"),
            }
            # §14.24: the uncertainty interval on the category mean.
            if value is not None:
                import math as _math
                p = value
                entry["ci95_low"] = round(max(0.0, p - 1.96 * _math.sqrt(
                    p * (1 - p) / scored)), 4)
                entry["ci95_high"] = round(min(1.0, p + 1.96 * _math.sqrt(
                    p * (1 - p) / scored)), 4)
            else:
                entry["ci95_low"] = entry["ci95_high"] = None
            entry["avg_latency_ms"] = (round(c["latency_ms_sum"] /
                                             c["latency_n"], 1)
                                       if c["latency_n"] else None)
            entry["avg_cost_units"] = (round(c["cost_units_sum"] /
                                             c["cost_n"], 3)
                                       if c["cost_n"] else None)
            out[cat] = entry
        return {"categories": out}

    async def logging_health(self) -> Dict[str, Any]:
        """§14.4: coverage/health of the log — decisions without feedback
        are EXPLICIT, never silently dropped."""
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN feedback_at IS NOT NULL THEN 1 ELSE 0 END) AS fed, "
            "COUNT(DISTINCT selected_action) AS arms "
            "FROM routing_decisions")
        row = await cursor.fetchone()
        total = int(row["total"] or 0)
        fed = int(row["fed"] or 0)
        return {"total_decisions": total, "with_feedback": fed,
                "missing_feedback": total - fed,
                "distinct_actions": int(row["arms"] or 0)}


def _row_to_decision(row) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["eligible_actions"] = json.loads(d.pop("eligible_actions_json") or "[]")
    except (ValueError, KeyError):
        d["eligible_actions"] = []
    return d


_repo: Optional[RoutingDecisionRepository] = None


def set_routing_repo(repo) -> None:
    global _repo
    _repo = repo


def get_routing_repo() -> Optional[RoutingDecisionRepository]:
    return _repo
