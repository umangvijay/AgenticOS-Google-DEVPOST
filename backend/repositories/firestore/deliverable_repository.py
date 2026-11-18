"""Firestore deliverable repository (Phase 17 §17.6 — parity).

The same staged-lifecycle contract as the SQLite reference: candidates
are unpublished until the manifest lands; `delivered` requires the
content checksum; ownership is enforced on every read."""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now
from backend.models.deliverable import Deliverable, new_deliverable_id

COLLECTION = "deliverables"
KEY_COLLECTION = "deliverable_keys"


def _doc_to_deliverable(data: Dict[str, Any], doc_id: str) -> Deliverable:
    return Deliverable(
        deliverable_id=doc_id,
        deliverable_key=data.get("deliverable_key") or "",
        revision=int(data.get("revision") or 1),
        parent_revision=data.get("parent_revision"),
        tenant_id=data.get("tenant_id") or "",
        owner_user_id=data.get("owner_user_id") or "",
        run_id=data.get("run_id"),
        task_id=data.get("task_id"),
        deliverable_type=data.get("deliverable_type") or "",
        mime_type=data.get("mime_type") or "",
        file_manifest=data.get("file_manifest") or [],
        lineage=data.get("lineage") or {},
        verification=data.get("verification") or {},
        lifecycle_state=data.get("lifecycle_state") or "generated",
        access_policy=data.get("access_policy") or "owner_only",
        content_checksum=data.get("content_checksum") or "",
        created_at=int(data.get("created_at") or 0),
        updated_at=int(data.get("updated_at") or 0),
    )


class FirestoreDeliverableRepository:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def create_candidate(self, d: Deliverable, *,
                               deliverable_key: Optional[str] = None) -> Deliverable:
        from google.cloud import firestore as fs_types
        db = await self._get_db()
        now = epoch_now()
        key_col = db.collection(KEY_COLLECTION)
        col = db.collection(COLLECTION)
        d.deliverable_key = deliverable_key or f"dlvk_{uuid.uuid4().hex[:16]}"
        key_ref = key_col.document(d.deliverable_key)

        @fs_types.async_transactional
        async def allocate(transaction):
            snap = await key_ref.get(transaction=transaction)
            if snap.exists:
                current = int((snap.to_dict() or {}).get("current_revision") or 0)
                d.revision = current + 1
                d.parent_revision = current
                transaction.update(key_ref, {"current_revision": d.revision})
            else:
                d.revision = 1
                d.parent_revision = None
                transaction.create(key_ref, {
                    "deliverable_key": d.deliverable_key,
                    "tenant_id": d.tenant_id,
                    "owner_user_id": d.owner_user_id,
                    "current_revision": 1,
                    "created_at": now})
            d.deliverable_id = new_deliverable_id()
            d.created_at = d.updated_at = now
            transaction.create(col.document(d.deliverable_id), {
                "deliverable_key": d.deliverable_key,
                "revision": d.revision,
                "parent_revision": d.parent_revision,
                "tenant_id": d.tenant_id,
                "owner_user_id": d.owner_user_id,
                "run_id": d.run_id, "task_id": d.task_id,
                "deliverable_type": d.deliverable_type,
                "mime_type": d.mime_type,
                "file_manifest": d.file_manifest,
                "content_checksum": "",
                "lineage": d.lineage,
                "verification": d.verification,
                "lifecycle_state": "generated",
                "access_policy": d.access_policy,
                "created_at": now, "updated_at": now})
            return d

        return await allocate(db.transaction())

    async def get(self, deliverable_id: str) -> Optional[Deliverable]:
        db = await self._get_db()
        snap = await db.collection(COLLECTION).document(deliverable_id).get()
        if not snap.exists:
            return None
        return _doc_to_deliverable(snap.to_dict() or {}, snap.id)

    async def get_for_owner(self, deliverable_id: str,
                            owner_user_id: str) -> Optional[Deliverable]:
        d = await self.get(deliverable_id)
        if d is None or d.owner_user_id != owner_user_id:
            return None   # wrong-user = missing (§12.28)
        return d

    async def list_for_owner(self, owner_user_id: str,
                             limit: int = 100) -> List[Deliverable]:
        db = await self._get_db()
        q = (db.collection(COLLECTION).where("owner_user_id", "==", owner_user_id)
             .where("lifecycle_state", "!=", "generated").limit(limit))
        rows = []
        async for snap in q.stream():
            rows.append(_doc_to_deliverable(snap.to_dict() or {}, snap.id))
        rows.sort(key=lambda d: d.updated_at, reverse=True)
        return rows

    async def transition(self, deliverable_id: str, target: str, *,
                         verification: Optional[Dict[str, Any]] = None,
                         file_manifest: Optional[List[Dict[str, Any]]] = None,
                         content_checksum: Optional[str] = None,
                         actor: str = "system") -> Deliverable:
        from backend.models.deliverable import LIFECYCLE_STATES
        if target not in LIFECYCLE_STATES:
            raise ValueError(f"unknown lifecycle state '{target}'")
        db = await self._get_db()
        ref = db.collection(COLLECTION).document(deliverable_id)
        now = epoch_now()

        @fs_types.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise ValueError("deliverable not found")
            data = snap.to_dict() or {}
            if target == "delivered" and content_checksum is None \
                    and not (data.get("file_manifest") or []):
                raise ValueError(
                    "cannot deliver without registered bytes (§17.7)")
            update: Dict[str, Any] = {"lifecycle_state": target,
                                      "updated_at": now}
            if verification is not None:
                update["verification"] = verification
            if file_manifest is not None:
                update["file_manifest"] = file_manifest
            if content_checksum is not None:
                update["content_checksum"] = content_checksum
            transaction.update(ref, update)

        await apply(db.transaction())
        return await self.get(deliverable_id)

    # ── §19.1 (C1 parity): review gate + publication jobs ──────────────

    async def review_with_revision_check(self, deliverable_id: str, *,
                                         expected_revision: int,
                                         mutate) -> Dict[str, Any]:
        """§18.4/§19.4 parity: the expected-revision check and the
        mutation run in ONE Firestore transaction."""
        from google.cloud import firestore as fs_types
        db = await self._get_db()
        d = await self.get(deliverable_id)
        if d is None:
            return {"conflict": False, "not_found": True}
        key_ref = db.collection(KEY_COLLECTION).document(d.deliverable_key)
        ref = db.collection(COLLECTION).document(deliverable_id)

        @fs_types.async_transactional
        async def apply(transaction):
            key_snap = await key_ref.get(transaction=transaction)
            current = int((key_snap.to_dict() or {}).get(
                "current_revision") or 0) if key_snap.exists else 0
            if current != int(expected_revision):
                return {"conflict": True, "current_revision": current,
                        "reviewed_revision": int(expected_revision)}
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return {"conflict": False, "not_found": True}
            data = snap.to_dict() or {}
            manifest = list(data.get("file_manifest") or [])
            new_manifest = mutate(manifest)
            from backend.repositories.deliverable_repository import (
                content_checksum_from_manifest,
            )
            checksum = content_checksum_from_manifest(new_manifest)
            transaction.update(ref, {
                "file_manifest": new_manifest,
                "content_checksum": checksum,
                "updated_at": epoch_now()})
            return {"conflict": False, "manifest": new_manifest,
                    "content_checksum": checksum}

        return await apply(db.transaction())

    async def create_publication_job(self, *, deliverable_id: str,
                                     owner_user_id: str,
                                     fail_after: Optional[str] = None,
                                     max_attempts: int = 5) -> str:
        import uuid as _uuid
        db = await self._get_db()
        job_id = f"pub_{_uuid.uuid4().hex[:20]}"
        await db.collection("publication_jobs").document(job_id).set({
            "job_id": job_id, "deliverable_id": deliverable_id,
            "owner_user_id": owner_user_id,
            "stage": "generation_allocated", "state": "pending",
            "attempts": 0, "max_attempts": max_attempts,
            "fail_after": fail_after, "last_error": None,
            "created_at": epoch_now(), "updated_at": epoch_now()})
        return job_id

    async def claim_publication_job(self, worker_id: str, *,
                                    lease_seconds: int = 120)             -> Optional[Dict[str, Any]]:
        from google.cloud import firestore as _fs
        db = await self._get_db()
        now = epoch_now()
        col = db.collection("publication_jobs")
        q = (col.where("state", "==", "pending")
             .where("attempts", "<", 5).limit(1))

        @_fs.async_transactional
        async def claim(transaction):
            snaps = [s async for s in q.stream()]
            if not snaps:
                return None
            snap = snaps[0]
            transaction.update(snap.reference, {
                "state": "claimed", "owner_worker_id": worker_id,
                "lease_expires_at": now + lease_seconds,
                "attempts": int((snap.to_dict() or {}).get("attempts") or 0) + 1,
                "updated_at": now})
            return snap.id

        job_id = await claim(db.transaction())
        if not job_id:
            return None
        snap = await col.document(job_id).get()
        d = dict(snap.to_dict() or {})
        d["job_id"] = job_id
        return d

    async def record_publication_progress(self, job_id: str, *,
                                          worker_id: str, stage: str,
                                          state: Optional[str] = None,
                                          error: Optional[str] = None) -> bool:
        from google.cloud import firestore as _fs
        db = await self._get_db()
        ref = db.collection("publication_jobs").document(job_id)

        @_fs.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False
            d = snap.to_dict() or {}
            if d.get("state") != "claimed" or \
                    d.get("owner_worker_id") != worker_id:
                return False
            update: Dict[str, Any] = {"stage": stage,
                                      "updated_at": epoch_now()}
            if state is not None:
                update["state"] = state
                if state == "pending":
                    update["lease_expires_at"] = None
                    update["owner_worker_id"] = None
            if error is not None:
                update["last_error"] = error
            transaction.update(ref, update)
            return True

        return bool(await apply(db.transaction()))

    async def publication_jobs_requiring_recovery(self, *,
                                                  limit: int = 20)             -> List[Dict[str, Any]]:
        db = await self._get_db()
        col = db.collection("publication_jobs")
        out = []
        for state in ("pending", "failed"):
            q = col.where("state", "==", state).limit(limit)
            async for snap in q.stream():
                d = dict(snap.to_dict() or {}, job_id=snap.id)
                if int(d.get("attempts") or 0) < int(
                        d.get("max_attempts") or 5):
                    out.append(d)
        out.sort(key=lambda j: j.get("created_at") or 0)
        return out[:limit]

    async def recover_publication(self, job: Dict[str, Any], *,
                                  worker_id: str) -> Dict[str, Any]:
        claimed = job
        if job.get("state") != "claimed" or \
                job.get("owner_worker_id") != worker_id:
            claimed = await self.claim_publication_job(worker_id)
            if not claimed or claimed.get("job_id") != job.get("job_id"):
                return {"recovered": False, "reason": "claim lost"}
        d = await self.get(job["deliverable_id"])
        if d is None:
            await self.record_publication_progress(
                job["job_id"], worker_id=worker_id, stage="abandoned",
                state="failed", error="deliverable record missing")
            return {"recovered": False, "reason": "record missing"}
        files = [{**f, "content": f.get("content", "")}
                 for f in (d.file_manifest or [])]
        from backend.services.deliverable_publisher import publish_deliverable
        tx = await publish_deliverable(
            repo=self, deliverable_id=d.deliverable_id, files=files,
            fail_after=claimed.get("fail_after"))
        if tx.failures or tx.stage != "completion_emitted":
            attempts = int(claimed.get("attempts") or 0)
            max_attempts = int(claimed.get("max_attempts") or 5)
            error = (tx.failures[-1]["error"] if tx.failures
                     else f"publication stopped at {tx.stage}")
            final = "failed" if attempts >= max_attempts else "pending"
            await self.record_publication_progress(
                job["job_id"], worker_id=worker_id, stage=tx.stage,
                state=final, error=error[:300])
            return {"recovered": False, "stage": tx.stage, "parked": final}
        await self.record_publication_progress(
            job["job_id"], worker_id=worker_id, stage=tx.stage,
            state="complete")
        return {"recovered": True, "stage": tx.stage,
                "job_id": job["job_id"]}

    async def current_revision_for(self, deliverable_key: str) -> int:
        db = await self._get_db()
        snap = await db.collection(KEY_COLLECTION).document(
            deliverable_key).get()
        return int((snap.to_dict() or {}).get("current_revision") or 0) \
            if snap.exists else 0
