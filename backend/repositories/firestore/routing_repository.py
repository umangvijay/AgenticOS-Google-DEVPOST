"""Firestore routing-decision repository (Phase 14 — parity).

The SAME append-only contract as the SQLite RoutingDecisionRepository:
deterministic ids, delayed verified feedback joined EXACTLY once (the
update is guarded on feedback_at IS NULL via a transaction), explicit
missing-feedback accounting.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now
from google.cloud import firestore as fs_types

logger = logging.getLogger(__name__)

COLLECTION = "routing_decisions"


def new_decision_id() -> str:
    return f"rd_{uuid.uuid4().hex[:20]}"


def _doc_to_decision(data: Dict[str, Any], decision_id: str) -> Dict[str, Any]:
    d = dict(data)
    d["decision_id"] = decision_id
    d["eligible_actions"] = list(d.pop("eligible_actions", []) or [])
    d.pop("eligible_actions_json", None)
    return d


class FirestoreRoutingDecisionRepository:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def log_decision(self, decision) -> Any:
        decision.created_at = decision.created_at or epoch_now()
        row = decision.to_row()
        doc = dict(row)
        doc["eligible_actions"] = json.loads(doc.pop("eligible_actions_json") or "[]")
        doc.pop("eligible_actions_json", None)
        await (await self._get_db()).collection(COLLECTION).document(
            decision.decision_id).create(doc)
        return decision

    async def attach_feedback(self, run_id: str, reward_class: str,
                              verified_success: Optional[bool],
                              evaluation_id: Optional[str] = None) -> int:
        """Join exactly once: the transaction re-reads each candidate
        decision and skips any that already carry feedback."""
        from google.cloud import firestore as fs_types
        db = await self._get_db()
        now = epoch_now()
        query = (db.collection(COLLECTION).where("run_id", "==", run_id)
                 .where("feedback_at", "==", None).limit(200))
        refs = [snap.reference async for snap in query.stream()]
        joined = 0

        @fs_types.async_transactional
        async def apply(transaction):
            nonlocal joined
            for ref in refs:
                snap = await ref.get(transaction=transaction)
                if not snap.exists:
                    continue
                if (snap.to_dict() or {}).get("feedback_at") is not None:
                    continue
                transaction.update(ref, {
                    "reward_class": reward_class,
                    "reward_verified_success": (
                        None if verified_success is None
                        else (1 if verified_success else 0)),
                    "feedback_at": now,
                    "evaluation_id": evaluation_id,
                })
                joined += 1

        await apply(db.transaction())
        return joined

    async def decisions_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        query = ((await self._get_db()).collection(COLLECTION)
                 .where("run_id", "==", run_id).limit(200))
        rows = []
        async for snap in query.stream():
            rows.append(_doc_to_decision(snap.to_dict() or {}, snap.id))
        rows.sort(key=lambda d: d.get("created_at") or 0)
        return rows

    async def logged_decisions(self, limit: int = 5000) -> List[Dict[str, Any]]:
        query = ((await self._get_db()).collection(COLLECTION)
                 .where("feedback_at", "!=", None).limit(limit))
        rows = []
        async for snap in query.stream():
            rows.append(_doc_to_decision(snap.to_dict() or {}, snap.id))
        rows.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
        return rows

    # ── §14.18/§14.19: durable reward events + policy marker + summary ──

    REWARD_COLLECTION = "reward_events"

    async def create_reward_event(self, *, decision_id: str,
                                  evaluation_id: str,
                                  reward_class: str, reward_value: float,
                                  model: str, context_key: str = "",
                                  latency_ms: Optional[float] = None,
                                  cost_units: Optional[float] = None,
                                  reward_version: str = "v1") -> Optional[str]:
        """§14.18: immutable reward event with the identity
        (decision, evaluation, version). The doc ID IS the identity — a
        concurrent duplicate create resolves to the SAME document."""
        import hashlib as _hl
        db = await self._get_db()
        identity = f"{decision_id}__{evaluation_id}__{reward_version}"
        event_id = "rew_" + _hl.sha256(identity.encode()).hexdigest()[:24]
        ref = db.collection(self.REWARD_COLLECTION).document(event_id)
        try:
            await ref.create({
                "event_id": event_id,
                "decision_id": decision_id,
                "evaluation_id": evaluation_id,
                "reward_version": reward_version,
                "reward_class": reward_class,
                "reward_value": float(reward_value),
                "model": model,
                "context_key": context_key,
                "latency_ms": latency_ms,
                "cost_units": cost_units,
                "consumed_by_contextual": 0,
                "consumed_by_global": 0,
                "quarantined": 0,
                "quarantine_reason": None,
                "created_at": epoch_now(),
            })
        except Exception as exc:
            # AlreadyExists → the pre-existing event IS the answer.
            if "already exists" not in str(exc).lower():
                raise
        return event_id

    async def unconsumed_reward_events(self, consumer: str,
                                       reward_version: str = "v1",
                                       limit: int = 100) -> List[Dict[str, Any]]:
        col = ("consumed_by_contextual" if consumer == "contextual"
               else "consumed_by_global")
        db = await self._get_db()
        query = (db.collection(self.REWARD_COLLECTION)
                 .where("reward_version", "==", reward_version)
                 .where(col, "==", 0).limit(limit))
        rows = []
        async for snap in query.stream():
            rows.append(dict(snap.to_dict() or {}, event_id=snap.id))
        rows.sort(key=lambda r: r.get("created_at") or 0)
        return rows

    async def mark_reward_consumed(self, event_id: str, consumer: str) -> bool:
        col = ("consumed_by_contextual" if consumer == "contextual"
               else "consumed_by_global")
        db = await self._get_db()
        ref = db.collection(self.REWARD_COLLECTION).document(event_id)

        @fs_types.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False
            if int((snap.to_dict() or {}).get(col) or 0) == 1:
                return False
            transaction.update(ref, {col: 1})
            return True

        return bool(await apply(db.transaction()))

    async def mark_policy_applied(self, decision_id: str) -> None:
        db = await self._get_db()
        await db.collection(COLLECTION).document(decision_id).update(
            {"policy_applied": 1})

    async def summary_by_category(self, *, limit: int = 5000) -> Dict[str, Any]:
        """§14.2/§14.19: the same measured report as the SQLite repo,
        computed from the streamed decisions."""
        db = await self._get_db()
        rows = []
        async for snap in db.collection(COLLECTION).limit(limit).stream():
            rows.append(_doc_to_decision(snap.to_dict() or {}, snap.id))
        rows.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
        cats: Dict[str, Dict[str, Any]] = {}
        from backend.services.offline_policy_evaluation import REWARD_WEIGHTS
        import math as _math
        for d in rows:
            cat = d.get("task_category") or "workflow"
            c = cats.setdefault(cat, {
                "decisions": 0, "scored": 0, "weight": 0.0,
                "latency_ms_sum": 0.0, "latency_n": 0,
                "cost_units_sum": 0.0, "cost_n": 0, "by_class": {}})
            c["decisions"] += 1
            cls = d.get("reward_class") or "missing"
            c["by_class"][cls] = c["by_class"].get(cls, 0) + 1
            if cls == "missing":
                continue
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
                "decisions": c["decisions"], "scored": scored,
                "missing_feedback": c["by_class"].get("missing", 0),
                "by_class": c["by_class"],
                "verified_quality": round(value, 4) if value is not None else None,
                "confidence": ("none" if not scored else
                               "low" if scored < 10 else
                               "medium" if scored < 30 else "high"),
            }
            if value is not None:
                entry["ci95_low"] = round(max(0.0, value - 1.96 * _math.sqrt(
                    value * (1 - value) / scored)), 4)
                entry["ci95_high"] = round(min(1.0, value + 1.96 * _math.sqrt(
                    value * (1 - value) / scored)), 4)
            else:
                entry["ci95_low"] = entry["ci95_high"] = None
            entry["avg_latency_ms"] = (round(c["latency_ms_sum"] / c["latency_n"], 1)
                                       if c["latency_n"] else None)
            entry["avg_cost_units"] = (round(c["cost_units_sum"] / c["cost_n"], 3)
                                       if c["cost_n"] else None)
            out[cat] = entry
        return {"categories": out}

    async def logging_health(self) -> Dict[str, Any]:
        db = await self._get_db()
        total = fed = 0
        arms = set()
        async for snap in db.collection(COLLECTION).limit(5000).stream():
            d = snap.to_dict() or {}
            total += 1
            if d.get("feedback_at") is not None:
                fed += 1
            if d.get("selected_action"):
                arms.add(d["selected_action"])
        return {"total_decisions": total, "with_feedback": fed,
                "missing_feedback": total - fed, "distinct_actions": len(arms)}
