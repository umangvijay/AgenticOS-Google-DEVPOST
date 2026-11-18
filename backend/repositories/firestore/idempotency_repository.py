from typing import Optional, Any, Dict
from datetime import datetime, timezone
from google.cloud import firestore
from backend.repositories.base import BaseIdempotencyRepository
from backend.repositories.firestore.database import FirestoreDB

class FirestoreIdempotencyRepository(BaseIdempotencyRepository):
    """Firestore implementation of Idempotency Ledger with true transactional locks."""

    async def get_entry(self, key: str) -> Optional[Dict[str, Any]]:
        """Read one ledger entry without claiming it (§29.19 revision)."""
        snap = await self._col().document(key).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        return {"workflow_id": data.get("workflow_id"),
                "task_id": data.get("task_id"),
                "status": data.get("status"),
                "result_payload": data.get("result_payload")}

    async def release_replay_key(self, key: str) -> bool:
        """§4.16: release a delivery's replay identity when command
        acceptance failed — the sender's retry can be accepted."""
        db = await self._get_db()
        ref = db.collection("idempotency_ledger").document(key)
        snap = await ref.get()
        if not snap.exists:
            return False
        status = (snap.to_dict() or {}).get("status")
        if status not in ("running", "paused"):
            return False
        await ref.delete()
        return True

    async def _get_db(self):
        return await FirestoreDB.get_client()

    async def atomic_insert(self, key: str, workflow_id: str, task_id: str) -> bool:
        db = await self._get_db()
        doc_ref = db.collection("idempotency_ledger").document(key)

        @firestore.async_transactional
        async def insert_in_transaction(transaction, ref):
            snapshot = await ref.get(transaction=transaction)
            if snapshot.exists and (snapshot.to_dict() or {}).get("status") != "paused":
                return False
            now = datetime.now(timezone.utc).isoformat()
            transaction.set(
                ref,
                {
                    "idempotency_key": key,
                    "workflow_id": workflow_id,
                    "task_id": task_id,
                    "status": "running",
                    "result_payload": None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            return True

        transaction = db.transaction()
        return await insert_in_transaction(transaction, doc_ref)

    async def get_record(self, key: str) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        doc = await db.collection("idempotency_ledger").document(key).get()
        if doc.exists:
            return doc.to_dict() or {}
        return None

    async def commit_success(self, key: str, result_payload: str) -> None:
        await self.mark_completed(key, result_payload)

    async def commit_failure(self, key: str) -> None:
        await self.mark_failed(key)

    async def commit_uncertain(self, key: str) -> None:
        db = await self._get_db()
        ref = db.collection("idempotency_ledger").document(key)

        @firestore.async_transactional
        async def quarantine(transaction):
            snapshot = await ref.get(transaction=transaction)
            if (snapshot.to_dict() or {}).get("status") == "running":
                transaction.update(ref, {"status": "uncertain", "updated_at": datetime.now(timezone.utc).isoformat()})

        await quarantine(db.transaction())

    async def commit_pause(self, key: str) -> None:
        db = await self._get_db()
        await db.collection("idempotency_ledger").document(key).update({
            "status": "paused", "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    async def sweep_stale_running(self, older_than_seconds: int = 1800) -> int:
        """Quarantine 'running' entries whose lease (updated_at) has expired —
        same semantics as the SQLite implementation (audit F07)."""
        db = await self._get_db()
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        query = db.collection("idempotency_ledger").where("status", "==", "running").where("updated_at", "<", cutoff)
        swept = 0
        async for snap in query.stream():
            await snap.reference.update({"status": "uncertain", "updated_at": datetime.now(timezone.utc).isoformat()})
            swept += 1
        return swept

    async def reconcile_uncertain(self, key: str, outcome: str, result_payload: Optional[str] = None) -> None:
        """Deliberate resolution of an uncertain outcome; refuses anything
        that is not currently 'uncertain'."""
        if outcome not in ("completed", "failed"):
            raise ValueError("outcome must be 'completed' or 'failed'")
        db = await self._get_db()
        ref = db.collection("idempotency_ledger").document(key)

        @firestore.async_transactional
        async def resolve(transaction):
            snapshot = await ref.get(transaction=transaction)
            data = snapshot.to_dict() or {}
            if not snapshot.exists or data.get("status") != "uncertain":
                raise ValueError("Only 'uncertain' ledger entries can be reconciled")
            if outcome == "completed":
                transaction.update(ref, {"status": "completed", "result_payload": result_payload or "{}",
                                         "updated_at": datetime.now(timezone.utc).isoformat()})
            else:
                transaction.update(ref, {"status": "failed", "updated_at": datetime.now(timezone.utc).isoformat()})

        await resolve(db.transaction())

    async def reset_failed_task(self, workflow_id: str, task_id: str) -> None:
        db = await self._get_db()
        query = db.collection("idempotency_ledger").where("workflow_id", "==", workflow_id)
        async for doc in query.stream():
            data = doc.to_dict() or {}
            if data.get("task_id") != task_id:
                continue
            @firestore.async_transactional
            async def reset(transaction, ref):
                snapshot = await ref.get(transaction=transaction)
                if (snapshot.to_dict() or {}).get("status") == "failed":
                    transaction.update(ref, {"status": "paused"})
            await reset(db.transaction(), doc.reference)

    async def claim_execution(self, idempotency_key: str, workflow_id: str, task_id: str) -> Optional[Any]:
        """
        Attempts to atomically claim execution.
        Returns None if claimed successfully (i.e., we are the first worker).
        If already running, raises ValueError("already_running").
        If already completed, returns the cached result_payload.
        """
        db = await self._get_db()
        doc_ref = db.collection("idempotency_ledger").document(idempotency_key)

        @firestore.async_transactional
        async def claim_in_transaction(transaction, ref):
            snapshot = await ref.get(transaction=transaction)
            if snapshot.exists:
                data = snapshot.to_dict() or {}
                if data.get("status") == "completed":
                    return data.get("result_payload")
                else:
                    if data.get("status") != "paused":
                        raise ValueError("Execution blocked; reconciliation or explicit retry required")

            transaction.set(ref, {
                "idempotency_key": idempotency_key,
                "workflow_id": workflow_id,
                "task_id": task_id,
                "status": "running",
                "result_payload": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            return None

        transaction = db.transaction()
        return await claim_in_transaction(transaction, doc_ref)

    async def mark_completed(self, idempotency_key: str, result_payload: Any) -> None:
        db = await self._get_db()
        doc_ref = db.collection("idempotency_ledger").document(idempotency_key)
        await doc_ref.update({
            "status": "completed",
            "result_payload": result_payload,
            "updated_at": datetime.now(timezone.utc).isoformat()
        })

    async def mark_failed(self, idempotency_key: str) -> None:
        db = await self._get_db()
        doc_ref = db.collection("idempotency_ledger").document(idempotency_key)
        await doc_ref.update({
            "status": "failed",
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
