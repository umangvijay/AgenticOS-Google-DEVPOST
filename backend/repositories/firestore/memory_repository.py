import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector
from backend.repositories.base import BaseMemoryRepository
from backend.repositories.firestore.database import FirestoreDB

class FirestoreMemoryRepository(BaseMemoryRepository):
    """Firestore implementation using native Vector Search capabilities."""

    async def _get_db(self):
        return await FirestoreDB.get_client()

    async def store_memory(
        self,
        user_id: str,
        content: str,
        memory_type: str,
        metadata: Dict[str, Any],
        embedding: List[float],
    ) -> str:
        meta = dict(metadata or {})
        db = await self._get_db()
        # §17.4 (finding C): create-or-get TRANSACTIONAL — the query and
        # the create run inside one Firestore transaction, so concurrent
        # duplicate events resolve to ONE logical memory; the identity
        # lives at TOP LEVEL (where the lookup reads it), not only in
        # metadata.
        if meta.get("source_event_id"):
            from google.cloud import firestore as _fs
            col = db.collection("memories")
            q = (col.where("user_id", "==", user_id)
                 .where("source_event_id", "==", meta["source_event_id"])
                 .limit(1))

            @_fs.async_transactional
            async def create_or_get(transaction):
                existing = [snap async for snap in q.stream()]
                if existing:
                    return existing[0].id
                doc_id = str(uuid.uuid4())
                doc = dict(meta)
                doc.update({
                    "memory_type": memory_type, "content": content,
                    "embedding": embedding, "user_id": user_id,
                    "source_event_id": meta["source_event_id"],
                    "scope": meta.get("scope", "personal"),
                    "scope_ref": meta.get("scope_ref", ""),
                    "verification_status": meta.get(
                        "verification_status", "unverified"),
                    "sensitivity": meta.get("sensitivity", "normal"),
                    "deletion_state": meta.get("deletion_state", "active"),
                    "version": int(meta.get("version", 1)),
                })
                transaction.create(col.document(doc_id), doc)
                return doc_id

            return await create_or_get(db.transaction())
        memory_id = str(uuid.uuid4())
        meta["memory_type"] = memory_type
        await self.add_memory(memory_id, user_id, content, embedding, meta)
        return memory_id

    async def add_memory(self, memory_id: str, user_id: str, content: str, embedding: List[float], metadata: Dict[str, Any] = None) -> None:
        db = await self._get_db()
        doc_ref = db.collection("memories").document(memory_id)

        data = {
            "memory_id": memory_id,
            "id": memory_id,
            "user_id": user_id,
            "content": content,
            "embedding": Vector(embedding) if embedding else None,
            "metadata": metadata or {},
            "memory_type": (metadata or {}).get("memory_type") or None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        await doc_ref.set(data)

    async def search_memory(
        self,
        user_id: str,
        query_embedding: List[float],
        memory_type: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        db = await self._get_db()

        query = db.collection("memories").where("user_id", "==", user_id)
        if memory_type:
            query = query.where("memory_type", "==", memory_type)

        vector_query = query.find_nearest(
            vector_field="embedding",
            query_vector=Vector(query_embedding),
            distance_measure=firestore.DistanceMeasure.COSINE,
            limit=limit,
            distance_result_field="vector_distance",
        )

        results = []
        async for doc in vector_query.stream():
            data = doc.to_dict() or {}
            data.pop("embedding", None)
            data.setdefault("id", data.get("memory_id") or doc.id)
            data["similarity_score"] = 1.0 - float(data.get("vector_distance") or 0.0)
            results.append(data)

        return results

    async def delete_memory(self, user_id: str, memory_id: str) -> bool:
        db = await self._get_db()
        doc_ref = db.collection("memories").document(memory_id)

        doc = await doc_ref.get()
        if not doc.exists:
            return False

        data = doc.to_dict() or {}
        if data.get("user_id") != user_id:
            return False

        await doc_ref.delete()
        return True

    async def update_memory(self, user_id: str, memory_id: str, content: str, embedding: List[float]) -> bool:
        db = await self._get_db()
        doc_ref = db.collection("memories").document(memory_id)
        doc = await doc_ref.get()
        if not doc.exists:
            return False
        data = doc.to_dict() or {}
        if data.get("user_id") != user_id:
            return False
        await doc_ref.update(
            {
                "content": content,
                "embedding": Vector(embedding) if embedding else None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return True

    async def list_memories(
        self,
        user_id: str,
        memory_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        db = await self._get_db()
        col = db.collection("memories").where("user_id", "==", user_id)
        if memory_type:
            col = col.where("memory_type", "==", memory_type)

        def _missing_index(exc: BaseException) -> bool:
            name = type(exc).__name__
            msg = str(exc).lower()
            return (
                name == "FailedPrecondition"
                or "failed_precondition" in msg
                or "requires an index" in msg
                or "the query requires an index" in msg
            )

        try:
            query = col.order_by("created_at", direction=firestore.Query.DESCENDING)
            rows: List[Dict[str, Any]] = []
            async for doc in query.stream():
                rows.append(doc.to_dict() or {})
        except Exception as e:
            if not _missing_index(e):
                raise
            rows = []
            async for doc in col.stream():
                rows.append(doc.to_dict() or {})
            rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)

        start = max(0, offset)
        out = []
        for data in rows[start : start + max(1, limit)]:
            data = dict(data)
            data.pop("embedding", None)
            data.setdefault("id", data.get("memory_id") or data.get("id"))
            out.append(data)
        return out

    # ── §17.4 (finding C): durable lifecycle contracts (parity) ────────

    async def supersede_older_for_subject(self, user_id: str, subject_key: str,
                                          new_id: str) -> int:
        db = await self._get_db()
        q = (db.collection("memories").where("user_id", "==", user_id)
             .where("subject_key", "==", subject_key).limit(200))
        n = 0
        async for snap in q.stream():
            d = snap.to_dict() or {}
            if d.get("superseded_by") or snap.id == new_id:
                continue
            meta = dict(d.get("metadata") or {})
            meta["superseded_by"] = new_id
            meta["superseded_at"] = int(__import__("time").time())
            await snap.reference.update({
                "superseded_by": new_id, "metadata": meta})
            n += 1
        return n

    async def register_dependency(self, memory_id: str, *, derived_kind: str,
                                  derived_id: str) -> str:
        import uuid as _uuid
        db = await self._get_db()
        edge_id = f"mde_{_uuid.uuid4().hex[:20]}"
        await db.collection("memory_dependency_edges").document(edge_id).set({
            "edge_id": edge_id, "memory_id": memory_id,
            "derived_kind": derived_kind, "derived_id": derived_id,
            "created_at": int(__import__("time").time())})
        return edge_id

    async def dependents_of(self, memory_id: str) -> List[Dict[str, Any]]:
        db = await self._get_db()
        q = db.collection("memory_dependency_edges").where(
            "memory_id", "==", memory_id).limit(200)
        rows = []
        async for snap in q.stream():
            rows.append(dict(snap.to_dict() or {}, edge_id=snap.id))
        return rows

    async def create_cleanup_job(self, *, memory_id: str, user_id: str,
                                 dependents: List[Dict[str, Any]]) -> str:
        import uuid as _uuid
        db = await self._get_db()
        job_id = f"mcj_{_uuid.uuid4().hex[:20]}"
        await db.collection("memory_cleanup_jobs").document(job_id).set({
            "job_id": job_id, "memory_id": memory_id, "user_id": user_id,
            "state": "pending", "dependents": dependents or [],
            "attempts": 0,
            "created_at": int(__import__("time").time()),
            "updated_at": int(__import__("time").time())})
        return job_id

    async def claim_cleanup_job(self, worker_id: str, *,
                                lease_seconds: int = 120) -> Optional[Dict[str, Any]]:
        from google.cloud import firestore as _fs
        db = await self._get_db()
        now = int(__import__("time").time())
        col = db.collection("memory_cleanup_jobs")
        q = col.where("state", "==", "pending").limit(1)

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
        d["dependents"] = d.get("dependents") or []
        return d

    async def record_cleanup_progress(self, job_id: str, *, worker_id: str,
                                      state: str,
                                      error: Optional[str] = None) -> bool:
        from google.cloud import firestore as _fs
        db = await self._get_db()
        ref = db.collection("memory_cleanup_jobs").document(job_id)

        @_fs.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False
            d = snap.to_dict() or {}
            if d.get("state") != "claimed" or \
                    d.get("owner_worker_id") != worker_id:
                return False
            transaction.update(ref, {"state": state, "last_error": error,
                                     "updated_at": int(__import__("time").time())})
            return True

        return bool(await apply(db.transaction()))

    async def get_memory(self, user_id: str, memory_id: str) -> Optional[Dict[str, Any]]:
        """§16.30 parity: the direct, owner-scoped lookup."""
        db = await self._get_db()
        snap = await db.collection("memories").document(memory_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        if data.get("user_id") != user_id:
            return None   # ownership-first on every path
        data["id"] = snap.id
        data.setdefault("scope", "personal")
        data.setdefault("verification_status", "unverified")
        data.setdefault("deletion_state", "active")
        return data

    async def get_memories_by_category(self, user_id: str, category: str, limit: int = 50) -> List[Dict[str, Any]]:
        db = await self._get_db()
        query = db.collection("memories")\
                  .where("user_id", "==", user_id)\
                  .where("metadata.category", "==", category)\
                  .order_by("created_at", direction=firestore.Query.DESCENDING)\
                  .limit(limit)

        results = []
        async for doc in query.stream():
            data = doc.to_dict() or {}
            data.pop("embedding", None)
            results.append(data)

        return results
