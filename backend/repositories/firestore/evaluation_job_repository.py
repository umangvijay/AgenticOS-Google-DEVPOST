"""Firestore evaluation-job repository (Phase 13 §13.2 — parity).

The SAME crash-safe contract as the SQLite EvaluationJobRepository:
create-or-get identity (run_id, evaluator_version) via a deterministic
anchor transaction, CAS lease claims with live-lease checks, lease-owner
fencing on completion, visible exhausted parking, and reconciliation for
terminal runs discovered without a job.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

from google.cloud import firestore as fs_types

from backend.models.admission import epoch_now

logger = logging.getLogger(__name__)

COLLECTION = "evaluation_jobs"
ANCHORS = "evaluation_job_anchors"


def _new_job_id() -> str:
    return f"evj_{uuid.uuid4().hex[:20]}"


class EvaluationJobError(Exception):
    def __init__(self, message: str, job_id: str = ""):
        super().__init__(message)
        self.job_id = job_id


class FirestoreEvaluationJobRepository:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def enqueue(self, run_id: str, *,
                      evaluator_version: str = "3.0.0",
                      reason: str = "run_terminal") -> Dict[str, Any]:
        db = await self._get_db()
        now = epoch_now()
        anchor_ref = db.collection(ANCHORS).document(
            f"{run_id}__{evaluator_version}")
        job_id = _new_job_id()
        doc = {"run_id": run_id, "evaluator_version": evaluator_version,
               "reason": reason, "state": "pending", "state_version": 1,
               "attempts": 0, "owner_worker_id": None,
               "lease_expires_at": None, "evaluation_id": None,
               "last_error": None, "created_at": now, "updated_at": now}

        @fs_types.async_transactional
        async def create(transaction):
            anchor = await anchor_ref.get(transaction=transaction)
            if anchor.exists:
                existing_id = (anchor.to_dict() or {}).get("job_id")
                snap = await db.collection(COLLECTION).document(
                    existing_id).get(transaction=transaction)
                if snap.exists:
                    d = dict(snap.to_dict() or {})
                    d["job_id"] = existing_id
                    return d
            transaction.create(db.collection(COLLECTION).document(job_id), doc)
            transaction.set(anchor_ref, {"job_id": job_id})
            d = dict(doc)
            d["job_id"] = job_id
            return d

        return await create(db.transaction())

    async def _get(self, job_id: str) -> Optional[Dict[str, Any]]:
        snap = await (await self._get_db()).collection(COLLECTION).document(
            job_id).get()
        if not snap.exists:
            return None
        d = dict(snap.to_dict() or {})
        d["job_id"] = job_id
        return d

    async def claim_one(self, job_id: str, worker_id: str, *,
                        lease_seconds: int = 120) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(job_id)

        @fs_types.async_transactional
        async def claim(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            if data.get("state") not in ("pending", "leased"):
                return None
            if data.get("state") == "leased" and \
                    int(data.get("lease_expires_at") or 0) > now:
                return None
            transaction.update(ref, {
                "state": "leased", "owner_worker_id": worker_id,
                "lease_expires_at": now + lease_seconds,
                "state_version": int(data.get("state_version") or 1) + 1,
                "updated_at": now,
            })
            return True

        if await claim(db.transaction()) is not True:
            return None
        return await self._get(job_id)

    async def claim(self, worker_id: str, *,
                    lease_seconds: int = 120,
                    batch: int = 5) -> List[Dict[str, Any]]:
        db = await self._get_db()
        now = epoch_now()
        query = (db.collection(COLLECTION).where("state", "==", "pending")
                 .limit(batch))
        claimed = []
        for snap in [s async for s in query.stream()]:
            job = await self.claim_one(snap.id, worker_id,
                                       lease_seconds=lease_seconds)
            if job:
                claimed.append(job)
        return claimed

    async def complete(self, job_id: str, *, evaluation_id: str,
                       owner_worker_id: str) -> None:
        """§14.G: the claim identity is MANDATORY — the fenced transaction
        refuses a non-owner and an expired lease alike."""
        if not owner_worker_id:
            raise EvaluationJobError(
                "owner_worker_id is required to complete a job", job_id)
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(job_id)

        @fs_types.async_transactional
        async def finish(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise EvaluationJobError("Job not found", job_id)
            data = snap.to_dict() or {}
            if (data.get("owner_worker_id") != owner_worker_id
                    or data.get("state") != "leased"
                    or int(data.get("lease_expires_at") or 0) <= now):
                raise EvaluationJobError(
                    "Stale worker: the lease is no longer yours", job_id)
            transaction.update(ref, {"state": "done",
                                     "evaluation_id": evaluation_id,
                                     "lease_expires_at": None,
                                     "updated_at": now})

        await finish(db.transaction())

    async def fail(self, job_id: str, error: str, *,
                   owner_worker_id: str,
                   max_attempts: int = 5) -> Dict[str, Any]:
        """§14.G: failure is FENCED — only the CURRENT lease owner."""
        if not owner_worker_id:
            raise EvaluationJobError(
                "owner_worker_id is required to fail a job", job_id)
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(job_id)

        @fs_types.async_transactional
        async def fail_tx(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise EvaluationJobError("Job not found", job_id)
            data = snap.to_dict() or {}
            state_now = data.get("state")
            if state_now == "leased":
                # The ownership fence: only the CURRENT live lease owner.
                if (data.get("owner_worker_id") != owner_worker_id
                        or int(data.get("lease_expires_at") or 0) <= now):
                    raise EvaluationJobError(
                        "Stale worker: the lease is no longer yours",
                        job_id)
            elif state_now not in ("pending", "exhausted"):
                raise EvaluationJobError(
                    "Stale worker: the job is not active", job_id)
            attempts = int(data.get("attempts") or 0) + 1
            state = "exhausted" if attempts >= max_attempts else "pending"
            transaction.update(ref, {
                "attempts": attempts, "last_error": error[:300],
                "state": state, "owner_worker_id": None,
                "lease_expires_at": None, "updated_at": now,
            })

        await fail_tx(db.transaction())
        return await self._get(job_id)

    async def fail_active_for_run(self, run_id: str, error: str, *,
                                  owner_worker_id: Optional[str] = None,
                                  max_attempts: int = 5) -> None:
        db = await self._get_db()
        query = (db.collection(COLLECTION)
                 .where("run_id", "==", run_id)
                 .where("state", "in", ["leased", "pending"]).limit(5))
        async for snap in query.stream():
            data = snap.to_dict() or {}
            if int(data.get("attempts") or 0) >= max_attempts:
                continue
            if data.get("state") == "leased" and (
                    not owner_worker_id
                    or data.get("owner_worker_id") != owner_worker_id):
                continue   # not the owner — the fence holds
            await self.fail(snap.id, error,
                            owner_worker_id=owner_worker_id or "reconciler",
                            max_attempts=max_attempts)

    async def drain(self, worker_id: str, *, batch: int = 5,
                    run_evaluation: Optional[Callable] = None) -> int:
        if run_evaluation is None:
            return 0
        processed = 0
        for job in await self.claim(worker_id, batch=batch):
            try:
                evaluation_id = await run_evaluation(job)
                await self.complete(job["job_id"],
                                    evaluation_id=evaluation_id,
                                    owner_worker_id=worker_id)
                processed += 1
            except Exception as exc:
                await self.fail(job["job_id"], str(exc),
                                owner_worker_id=worker_id)
        return processed

    async def reset_exhausted(self) -> int:
        db = await self._get_db()
        now = epoch_now()
        query = db.collection(COLLECTION).where("state", "==", "exhausted").limit(100)
        n = 0
        async for snap in query.stream():
            await snap.reference.update({"state": "pending", "attempts": 0,
                                         "updated_at": now})
            n += 1
        return n

    async def enqueue_unfinished_terminals(self, run_ids: List[str]) -> int:
        n = 0
        for run_id in run_ids or []:
            existing = await self._get_db()
            query = (existing.collection(COLLECTION)
                     .where("run_id", "==", run_id).limit(1))
            found = False
            async for _ in query.stream():
                found = True
            if not found:
                await self.enqueue(run_id, reason="reconciliation")
                n += 1
        return n

    async def health(self) -> Dict[str, Any]:
        db = await self._get_db()
        by_state: Dict[str, int] = {}
        async for snap in db.collection(COLLECTION).limit(1000).stream():
            state = (snap.to_dict() or {}).get("state") or "pending"
            by_state[state] = by_state.get(state, 0) + 1
        return {"by_state": by_state, "pending": by_state.get("pending", 0),
                "exhausted": by_state.get("exhausted", 0),
                "done": by_state.get("done", 0)}
