"""
AgentOS — SQLite Memory Repository

Vector similarity via cosine distance in Python.
Fine for <10k memories per user. Cloud uses Firestore native vector search.
Strict per-user isolation — no cross-user retrieval, ever.
"""

import json
import time
import math
import logging
import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from backend.repositories.base import BaseMemoryRepository
from backend.repositories.sqlite.database import DatabaseManager

logger = logging.getLogger(__name__)


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class SQLiteMemoryRepository(BaseMemoryRepository):

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def store_memory(
        self,
        user_id: str,
        content: str,
        memory_type: str,
        metadata: Dict[str, Any],
        embedding: List[float],
    ) -> str:
        memory_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        # Phase 16: lifecycle columns travel with the metadata (write-through
        # so SQL can filter superseded/expired without JSON parsing).
        # Phase 17 §17.4 (finding B): create-or-get ATOMICALLY inside
        # the write transaction — concurrent duplicate events resolve to
        # ONE logical memory (the pre-transaction read raced).
        if (metadata or {}).get("source_event_id"):
            async with self.db.write_transaction() as conn:
                cur = await conn.execute(
                    "SELECT id FROM memories WHERE user_id=? AND "
                    "source_event_id=?",
                    (user_id, metadata["source_event_id"]))
                existing = await cur.fetchone()
                if existing:
                    return existing["id"]
                memory_id = str(uuid.uuid4())
                now2 = datetime.now(timezone.utc).isoformat()
                await conn.execute(
                    """
                    INSERT INTO memories (id, user_id, content, memory_type,
                        metadata, embedding, created_at, updated_at,
                        memory_class, source, confidence, expires_at,
                        version, subject_key, scope, scope_ref,
                        verification_status, sensitivity, observed_at,
                        extraction_meta, source_event_id, deletion_state)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (memory_id, user_id, content, memory_type,
                     json.dumps(metadata), json.dumps(embedding), now2, now2,
                     (metadata or {}).get("memory_class", "factual"),
                     (metadata or {}).get("source", ""),
                     (metadata or {}).get("confidence"),
                     (metadata or {}).get("expires_at"),
                     int((metadata or {}).get("version", 1)),
                     (metadata or {}).get("subject_key"),
                     (metadata or {}).get("scope", "personal"),
                     (metadata or {}).get("scope_ref", ""),
                     (metadata or {}).get("verification_status", "unverified"),
                     (metadata or {}).get("sensitivity", "normal"),
                     (metadata or {}).get("observed_at"),
                     json.dumps((metadata or {}).get("extraction_meta", {})),
                     (metadata or {}).get("source_event_id"),
                     (metadata or {}).get("deletion_state", "active")))
                return memory_id
        await self.db.execute(
            """
            INSERT INTO memories (id, user_id, content, memory_type, metadata,
                                  embedding, created_at, updated_at,
                                  memory_class, source, confidence, expires_at,
                                  version, subject_key,
                                  scope, scope_ref, verification_status,
                                  sensitivity, observed_at, extraction_meta,
                                  source_event_id, deletion_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                user_id,
                content,
                memory_type,
                json.dumps(metadata),
                json.dumps(embedding),
                now,
                now,
                (metadata or {}).get("memory_class", "factual"),
                (metadata or {}).get("source", ""),
                (metadata or {}).get("confidence"),
                (metadata or {}).get("expires_at"),
                int((metadata or {}).get("version", 1)),
                (metadata or {}).get("subject_key"),
                (metadata or {}).get("scope", "personal"),
                (metadata or {}).get("scope_ref", ""),
                (metadata or {}).get("verification_status", "unverified"),
                (metadata or {}).get("sensitivity", "normal"),
                (metadata or {}).get("observed_at"),
                json.dumps((metadata or {}).get("extraction_meta", {})),
                (metadata or {}).get("source_event_id"),
                (metadata or {}).get("deletion_state", "active"),
            ),
        )
        await self.db.commit()
        return memory_id

    async def get_memory(self, user_id: str, memory_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        )
        if not row:
            return None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "content": row["content"],
            "memory_type": row["memory_type"],
            "memory_class": row.get("memory_class") or "factual",
            "source": row.get("source") or "",
            "confidence": row.get("confidence"),
            "expires_at": row.get("expires_at"),
            "version": row.get("version", 1),
            "subject_key": row.get("subject_key"),
            "superseded_by": row.get("superseded_by"),
            "scope": row.get("scope") or "personal",
            "scope_ref": row.get("scope_ref") or "",
            "verification_status": row.get("verification_status") or "unverified",
            "sensitivity": row.get("sensitivity") or "normal",
            "observed_at": row.get("observed_at"),
            "extraction_meta": json.loads(row.get("extraction_meta") or "{}"),
            "source_event_id": row.get("source_event_id"),
            "deletion_state": row.get("deletion_state") or "active",
            "metadata": json.loads(row.get("metadata") or "{}"),
            "created_at": row["created_at"],
        }

    async def search_memory(
        self,
        user_id: str,
        query_embedding: List[float],
        memory_type: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Full-table scan with Python cosine similarity.
        Acceptable for <10k rows per user in SQLite mode.
        In Firestore mode, native vector search handles this.
        """
        if memory_type:
            rows = await self.db.fetch_all(
                "SELECT * FROM memories WHERE user_id = ? AND memory_type = ?",
                (user_id, memory_type),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM memories WHERE user_id = ?",
                (user_id,),
            )

        scored = []
        for row in rows:
            embedding = json.loads(row["embedding"])
            score = _cosine_similarity(embedding, query_embedding)
            entry = {
                "id": row["id"],
                "user_id": row["user_id"],
                "content": row["content"],
                "memory_type": row["memory_type"],
                # Phase 16 lifecycle columns (filtering reads these).
                "memory_class": row.get("memory_class") or "factual",
                "source": row.get("source") or "",
                "expires_at": row.get("expires_at"),
                "subject_key": row.get("subject_key"),
                "superseded_by": row.get("superseded_by"),
                "metadata": json.loads(row.get("metadata", "{}")),
                "similarity_score": score,
                "created_at": row["created_at"],
            }
            scored.append(entry)

        # Sort by similarity descending
        scored.sort(key=lambda x: x["similarity_score"], reverse=True)
        return scored[:limit]

    async def list_memories(
        self,
        user_id: str,
        memory_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        if memory_type:
            rows = await self.db.fetch_all(
                "SELECT * FROM memories WHERE user_id = ? AND memory_type = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (user_id, memory_type, limit, offset),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            )
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "content": r["content"],
                "memory_type": r["memory_type"],
                "memory_class": r.get("memory_class") or "factual",
                "source": r.get("source") or "",
                "confidence": r.get("confidence"),
                "expires_at": r.get("expires_at"),
                "subject_key": r.get("subject_key"),
                "superseded_by": r.get("superseded_by"),
                # Phase 16 v37: the governed fields on EVERY list path —
                # scope/status/deletion state drive the retrieval filters.
                "scope": r.get("scope") or "personal",
                "scope_ref": r.get("scope_ref") or "",
                "verification_status": r.get("verification_status")
                or "unverified",
                "sensitivity": r.get("sensitivity") or "normal",
                "source_event_id": r.get("source_event_id"),
                "deletion_state": r.get("deletion_state") or "active",
                "metadata": json.loads(r.get("metadata") or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ── Phase 17 §17.4: durable lifecycle contracts ────────────────────

    async def supersede_older_for_subject(self, user_id: str, subject_key: str,
                                          new_id: str) -> int:
        """§16.4 via a REPO CONTRACT (no service-level repo.db access,
        failures loud). Deleting the replacement NEVER reactivates the
        old record: the superseded pointer stays (a dangling pointer
        keeps the older version dark)."""
        rows = await self.db.fetch_all(
            "SELECT id, metadata FROM memories WHERE user_id=? AND "
            "subject_key=? AND id!=? AND superseded_by IS NULL",
            (user_id, subject_key, new_id))
        n = 0
        for r in rows or []:
            meta = dict(json.loads(r["metadata"] or "{}"))
            meta["superseded_by"] = new_id
            meta["superseded_at"] = int(time.time())
            await self.db.execute(
                "UPDATE memories SET superseded_by=?, metadata=? "
                "WHERE id=?", (new_id, json.dumps(meta), r["id"]))
            n += 1
        await self.db.commit()
        return n

    async def register_dependency(self, memory_id: str, *, derived_kind: str,
                                  derived_id: str) -> str:
        """Persist the dependency edge — a restart must not erase that a
        report/cache/dataset depended on this memory."""
        import uuid as _uuid
        edge_id = f"mde_{_uuid.uuid4().hex[:20]}"
        await self.db.execute(
            """INSERT INTO memory_dependency_edges
               (edge_id, memory_id, derived_kind, derived_id, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(memory_id, derived_kind, derived_id) DO NOTHING""",
            (edge_id, memory_id, derived_kind, derived_id, int(time.time())))
        await self.db.commit()
        return edge_id

    async def dependents_of(self, memory_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT edge_id, memory_id, derived_kind, derived_id, created_at "
            "FROM memory_dependency_edges WHERE memory_id=?",
            (memory_id,))
        return [dict(r) for r in rows]

    async def create_cleanup_job(self, *, memory_id: str, user_id: str,
                                 dependents: List[Dict[str, Any]]) -> str:
        import uuid as _uuid
        job_id = f"mcj_{_uuid.uuid4().hex[:20]}"
        await self.db.execute(
            """INSERT INTO memory_cleanup_jobs
               (job_id, memory_id, user_id, state, dependents_json,
                created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?, ?)""",
            (job_id, memory_id, user_id, json.dumps(dependents or []),
             int(time.time()), int(time.time())))
        await self.db.commit()
        return job_id

    async def claim_cleanup_job(self, worker_id: str, *,
                                lease_seconds: int = 120) -> Optional[Dict[str, Any]]:
        now = int(time.time())
        async with self.db.write_transaction() as conn:
            cur = await conn.execute(
                "SELECT job_id FROM memory_cleanup_jobs "
                "WHERE state='pending' OR (state='claimed' AND "
                "lease_expires_at<=?) OR (state='failed' AND attempts<5) "
                "ORDER BY created_at LIMIT 1", (now,))
            row = await cur.fetchone()
            if not row:
                return None
            cur = await conn.execute(
                "UPDATE memory_cleanup_jobs SET state='claimed', "
                "owner_worker_id=?, lease_expires_at=?, attempts=attempts+1, "
                "updated_at=? WHERE job_id=? AND state IN "
                "('pending','claimed','failed') AND (lease_expires_at IS "
                "NULL OR lease_expires_at<=?)",
                (worker_id, now + lease_seconds, now, row["job_id"], now))
            if not cur.rowcount:
                return None
        row = await self.db.fetch_one(
            "SELECT * FROM memory_cleanup_jobs WHERE job_id=?",
            (row["job_id"],))
        d = dict(row)
        d["dependents"] = json.loads(d.pop("dependents_json") or "[]")
        return d

    async def record_cleanup_progress(self, job_id: str, *, worker_id: str,
                                      state: str,
                                      error: Optional[str] = None) -> bool:
        now = int(time.time())
        # A terminal state RELEASES the lease (a failed job re-enters the
        # queue immediately — the visible bounded retry, §21.19).
        lease_clear = ", lease_expires_at=NULL, owner_worker_id=NULL" \
            if state in ("failed", "complete") else ""
        cur = await self.db.execute(
            f"UPDATE memory_cleanup_jobs SET state=?, last_error=?, "
            f"updated_at=?{lease_clear} "
            f"WHERE job_id=? AND owner_worker_id=? AND state='claimed'",
            (state, error, now, job_id, worker_id))
        await self.db.commit()
        return bool(cur.rowcount)

    async def delete_memory(self, user_id: str, memory_id: str) -> bool:
        conn = await self.db.connection()
        cursor = await conn.execute(
            "DELETE FROM memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount > 0

    async def update_memory(self, user_id: str, memory_id: str, content: str, embedding: List[float]) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        conn = await self.db.connection()
        cursor = await conn.execute(
            """
            UPDATE memories
            SET content = ?, embedding = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (content, json.dumps(embedding), now, memory_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount > 0
