"""Firestore evaluation repository (Phase 12 §12.11/§12.12 — parity).

The SAME immutable-history contract as the SQLite EvaluationRepository:

- Create-or-get idempotency by (run_id, evaluator_version), carried by a
  deterministic identity document (evaluation_identities/{run}__{v})
  created with transaction.create() — concurrent duplicates contend on
  that one document; the loser's retry reads the original.
- Create-only results: no in-place rewrites. A new scorer version makes
  a NEW record linked to the previous one (previous_evaluation_id).
- §12.29: the summary aggregates over the SAME explicit recent window
  (order + limit first, then aggregate) as SQLite.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from backend.models.admission import epoch_now

logger = logging.getLogger(__name__)

COLLECTION = "evaluation_records"
IDENTITY_COLLECTION = "evaluation_identities"


def _identity_doc_id(run_id: Optional[str], evaluator_version: str) -> str:
    return f"{run_id}__{evaluator_version}"


class FirestoreEvaluationRepository:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def save_evaluation(self, record: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        from google.cloud import firestore as fs_types
        db = await self._get_db()
        record.setdefault("created_at_epoch", epoch_now())
        run_id = record.get("run_id")
        evaluator_version = record.get("evaluator_version", "1.0.0")
        identity_ref = db.collection(IDENTITY_COLLECTION).document(
            _identity_doc_id(run_id, evaluator_version))

        # Linkage: an intentional re-evaluation (new scorer) points at the
        # latest existing record for the run.
        if not record.get("previous_evaluation_id"):
            prior = await self.get_for_identity(run_id, None, latest_any=True)
            if prior is not None and \
                    prior.get("evaluator_version") != evaluator_version:
                record["previous_evaluation_id"] = prior["evaluation_id"]

        doc = dict(record)

        @fs_types.async_transactional
        async def create(transaction):
            existing = await identity_ref.get(transaction=transaction)
            if existing.exists:
                existing_id = (existing.to_dict() or {}).get("evaluation_id")
                snap = await db.collection(COLLECTION).document(
                    existing_id).get(transaction=transaction)
                if snap.exists:
                    return dict(snap.to_dict() or {}), False
                # Identity without a record (partial write) — recover it.
                transaction.set(db.collection(COLLECTION).document(
                    record["evaluation_id"]), doc)
                transaction.set(identity_ref, {
                    "evaluation_id": record["evaluation_id"],
                    "run_id": run_id, "evaluator_version": evaluator_version,
                })
                return doc, True
            transaction.create(db.collection(COLLECTION).document(
                record["evaluation_id"]), doc)
            transaction.create(identity_ref, {
                "evaluation_id": record["evaluation_id"],
                "run_id": run_id, "evaluator_version": evaluator_version,
            })
            return doc, True

        return await create(db.transaction())

    async def get_for_identity(self, run_id: Optional[str],
                               evaluator_version: Optional[str],
                               latest_any: bool = False) -> Optional[Dict[str, Any]]:
        if run_id is None:
            return None
        db = await self._get_db()
        if latest_any:
            query = (db.collection(COLLECTION).where("run_id", "==", run_id)
                     .limit(50))
            best = None
            async for snap in query.stream():
                d = dict(snap.to_dict() or {})
                d["evaluation_id"] = snap.id
                if best is None or (d.get("created_at_epoch") or 0) > \
                        (best.get("created_at_epoch") or 0):
                    best = d
            if best is not None:
                best["record"] = best
            return best
        if evaluator_version is None:
            return None
        snap = await db.collection(IDENTITY_COLLECTION).document(
            _identity_doc_id(run_id, evaluator_version)).get()
        if not snap.exists:
            return None
        return await self.get_evaluation(
            (snap.to_dict() or {}).get("evaluation_id"))

    async def get_evaluation(self, evaluation_id: str) -> Optional[Dict[str, Any]]:
        snap = await (await self._get_db()).collection(COLLECTION).document(
            evaluation_id).get()
        if not snap.exists:
            return None
        d = dict(snap.to_dict() or {})
        d["record"] = d
        d["evaluation_id"] = evaluation_id
        return d

    async def evaluations_for_run(self, run_id: str, *, limit: int = 50,
                                  offset: int = 0) -> List[Dict[str, Any]]:
        """§12.30: bounded retrieval; stable ordering (created_at_epoch,
        evaluation_id) matches the SQLite contract. Offset pagination on
        Firestore uses the NOT-IN-free id-bound scan."""
        query = ((await self._get_db()).collection(COLLECTION)
                 .where("run_id", "==", run_id)
                 .order_by("created_at_epoch")
                 .limit(limit))
        if offset:
            # Stable offset: skip via the ordering key of the offset-th row.
            skip = ((await self._get_db()).collection(COLLECTION)
                    .where("run_id", "==", run_id)
                    .order_by("created_at_epoch").limit(offset))
            last_key = None
            async for snap in skip.stream():
                last_key = (snap.to_dict() or {}).get("created_at_epoch")
            if last_key is not None:
                query = (query.start_after({"created_at_epoch": last_key}))
        rows = []
        async for snap in query.stream():
            d = dict(snap.to_dict() or {})
            d["record"] = d
            d["evaluation_id"] = snap.id
            rows.append(d)
        return rows

    async def evaluation_summary(self, limit: int = 100) -> Dict[str, Any]:
        """§12.29: window first (recent `limit` records), then aggregate —
        the same sampling semantics as the SQLite implementation."""
        query = ((await self._get_db()).collection(COLLECTION)
                 .order_by("created_at_epoch", direction="DESCENDING")
                 .limit(limit))
        total = successes = 0
        latency_sum = 0.0
        tokens = recoveries = 0
        async for snap in query.stream():
            d = snap.to_dict() or {}
            total += 1
            successes += 1 if d.get("success") else 0
            latency = d.get("latency_ms")
            latency_sum += float(latency) if latency is not None else 0.0
            tokens += int(d.get("token_usage_total") or 0)
            recoveries += int(d.get("recovery_attempts") or 0)
        return {
            "total": total,
            "success_rate": round(successes / total, 3) if total else 0,
            "avg_latency_ms": round(latency_sum / total, 1) if total else 0,
            "total_tokens": tokens,
            "total_recoveries": recoveries,
        }
