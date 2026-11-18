"""Deliverable repository (Phase 17 §17.6) — SQLite reference; the
Firestore parity follows the same contract. Staged publication states
(§17.7) live in the lifecycle column: a crash before publication leaves
an unpublished candidate, never a downloadable 'completed' artifact."""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now
from backend.models.deliverable import Deliverable, new_deliverable_id


def _row_to_deliverable(row) -> Deliverable:
    return Deliverable(
        deliverable_id=row["deliverable_id"],
        deliverable_key=row["deliverable_key"],
        revision=int(row["revision"]),
        parent_revision=row["parent_revision"],
        tenant_id=row["tenant_id"],
        owner_user_id=row["owner_user_id"],
        run_id=row.get("run_id"),
        task_id=row.get("task_id"),
        deliverable_type=row["deliverable_type"],
        mime_type=row.get("mime_type") or "",
        file_manifest=json.loads(row.get("file_manifest") or "[]"),
        lineage=json.loads(row.get("lineage") or "{}"),
        verification=json.loads(row.get("verification_json") or "{}"),
        lifecycle_state=row.get("lifecycle_state") or "generated",
        access_policy=row.get("access_policy") or "owner_only",
        content_checksum=row.get("content_checksum") or "",
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


class DeliverableRepository:
    def __init__(self, db):
        self.db = db

    async def create_candidate(self, d: Deliverable, *,
                               deliverable_key: Optional[str] = None) -> Deliverable:
        """§17.7 stage 1: allocate the durable GENERATION identity. A
        candidate is unpublished — a crash anywhere before `publish`
        leaves an unpublished candidate, never a 'completed' artifact."""
        now = epoch_now()
        d.deliverable_id = new_deliverable_id()
        d.deliverable_key = deliverable_key or f"dlvk_{uuid.uuid4().hex[:16]}"
        d.revision = 1
        d.created_at = d.updated_at = now
        async with self.db.write_transaction() as conn:
            cur = await conn.execute(
                "SELECT current_revision FROM deliverable_keys WHERE "
                "deliverable_key=?", (d.deliverable_key,))
            row = await cur.fetchone()
            if row:
                d.revision = int(row["current_revision"]) + 1
                d.parent_revision = int(row["current_revision"])
                await conn.execute(
                    "UPDATE deliverable_keys SET current_revision=? "
                    "WHERE deliverable_key=?",
                    (d.revision, d.deliverable_key))
            else:
                await conn.execute(
                    "INSERT INTO deliverable_keys (deliverable_key, "
                    "tenant_id, owner_user_id, current_revision, created_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (d.deliverable_key, d.tenant_id, d.owner_user_id, now))
                d.revision = 1
            await conn.execute(
                """INSERT INTO deliverables
                   (deliverable_id, deliverable_key, revision, parent_revision,
                    tenant_id, owner_user_id, run_id, task_id,
                    deliverable_type, mime_type, file_manifest,
                    content_checksum, lineage, verification_json,
                    lifecycle_state, access_policy, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (d.deliverable_id, d.deliverable_key, d.revision,
                 d.parent_revision, d.tenant_id, d.owner_user_id, d.run_id,
                 d.task_id, d.deliverable_type, d.mime_type,
                 json.dumps(d.file_manifest), "", json.dumps(d.lineage),
                 json.dumps(d.verification), "generated", d.access_policy,
                 now, now))
        return d

    async def get(self, deliverable_id: str) -> Optional[Deliverable]:
        row = await self.db.fetch_one(
            "SELECT * FROM deliverables WHERE deliverable_id=?",
            (deliverable_id,))
        return _row_to_deliverable(row) if row else None

    async def get_for_owner(self, deliverable_id: str,
                            owner_user_id: str) -> Optional[Deliverable]:
        """Ownership-first: a wrong-user lookup looks identical to
        missing (§12.28)."""
        row = await self.db.fetch_one(
            "SELECT * FROM deliverables WHERE deliverable_id=? AND "
            "owner_user_id=?", (deliverable_id, owner_user_id))
        return _row_to_deliverable(row) if row else None

    async def list_for_owner(self, owner_user_id: str,
                             limit: int = 100, offset: int = 0)             -> List[Deliverable]:
        rows = await self.db.fetch_all(
            "SELECT * FROM deliverables WHERE owner_user_id=? AND "
            "lifecycle_state != 'generated' ORDER BY updated_at DESC "
            "LIMIT ? OFFSET ?", (owner_user_id, limit, offset))
        return [_row_to_deliverable(r) for r in rows]

    async def transition(self, deliverable_id: str, target: str, *,
                         verification: Optional[Dict[str, Any]] = None,
                         file_manifest: Optional[List[Dict[str, Any]]] = None,
                         content_checksum: Optional[str] = None,
                         actor: str = "system") -> Deliverable:
        """The staged lifecycle (§17.7/§17.10): generated → stored →
        verification_completed → verification_passed/failed →
        review_completed → delivered. `delivered` requires the content
        checksum (the published bytes ARE the identity)."""
        from backend.models.deliverable import LIFECYCLE_STATES
        if target not in LIFECYCLE_STATES:
            raise ValueError(f"unknown lifecycle state '{target}'")
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cur = await conn.execute(
                "SELECT lifecycle_state, file_manifest, verification_json "
                "FROM deliverables WHERE deliverable_id=?",
                (deliverable_id,))
            row = await cur.fetchone()
            if not row:
                raise ValueError("deliverable not found")
            if target == "delivered" and content_checksum is None and \
                    not json.loads(row["file_manifest"] or "[]"):
                raise ValueError(
                    "cannot deliver without registered bytes (§17.7)")
            import json as _json
            await conn.execute(
                """UPDATE deliverables SET lifecycle_state=?, 
                   verification_json=COALESCE(?, verification_json),
                   file_manifest=COALESCE(?, file_manifest),
                   content_checksum=COALESCE(?, content_checksum),
                   updated_at=? WHERE deliverable_id=?""",
                (target,
                 _json.dumps(verification) if verification is not None else None,
                 _json.dumps(file_manifest) if file_manifest is not None else None,
                 content_checksum, now, deliverable_id))
        return await self.get(deliverable_id)

    async def current_revision_for(self, deliverable_key: str) -> int:
        row = await self.db.fetch_one(
            "SELECT current_revision FROM deliverable_keys WHERE "
            "deliverable_key=?", (deliverable_key,))
        return int(row["current_revision"]) if row else 0

    async def review_with_revision_check(self, deliverable_id: str, *,
                                         expected_revision: int,
                                         mutate) -> Dict[str, Any]:
        """§18.4 (C10): ATOMIC expected-version review — the check of the
        key's CURRENT revision and the mutation run inside ONE
        transaction. Two tabs reviewing the same revision cannot silently
        overwrite each other: the loser gets a structured conflict with
        the current revision. `mutate(record_dict) -> new_content_map`
        returns the modified file manifest entries."""
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cur = await conn.execute(
                "SELECT current_revision FROM deliverable_keys WHERE "
                "deliverable_key=(SELECT deliverable_key FROM "
                "deliverables WHERE deliverable_id=?)",
                (deliverable_id,))
            row = await cur.fetchone()
            current = int(row["current_revision"]) if row else 0
            if current != int(expected_revision):
                return {"conflict": True, "current_revision": current,
                        "reviewed_revision": int(expected_revision)}
            cur = await conn.execute(
                "SELECT * FROM deliverables WHERE deliverable_id=?",
                (deliverable_id,))
            drow = await cur.fetchone()
            if not drow:
                return {"conflict": False, "not_found": True}
            record = dict(drow)
            manifest = json.loads(record["file_manifest"] or "[]")
            new_manifest = mutate(manifest)
            content_checksum = content_checksum_from_manifest(new_manifest)
            cur = await conn.execute(
                "UPDATE deliverables SET file_manifest=?, "
                "content_checksum=?, updated_at=? WHERE deliverable_id=? "
                "AND revision=?",
                (json.dumps(new_manifest), content_checksum, now,
                 deliverable_id, expected_revision))
            if not cur.rowcount:
                return {"conflict": True, "current_revision": current}
            return {"conflict": False, "manifest": new_manifest,
                    "content_checksum": content_checksum}


    # ── Phase 18 C7/C8: durable publication jobs ───────────────────────

    async def create_publication_job(self, *, deliverable_id: str,
                                     owner_user_id: str,
                                     fail_after: Optional[str] = None,
                                     max_attempts: int = 5) -> str:
        import uuid as _uuid
        job_id = f"pub_{_uuid.uuid4().hex[:20]}"
        now = epoch_now()
        await self.db.execute(
            """INSERT INTO publication_jobs
               (job_id, deliverable_id, owner_user_id, stage, state,
                attempts, max_attempts, fail_after, created_at, updated_at)
               VALUES (?, ?, ?, 'generation_allocated', 'pending',
                       0, ?, ?, ?, ?)""",
            (job_id, deliverable_id, owner_user_id, max_attempts,
             fail_after, now, now))
        await self.db.commit()
        return job_id

    async def claim_publication_job(self, worker_id: str, *,
                                    lease_seconds: int = 120)             -> Optional[Dict[str, Any]]:
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cur = await conn.execute(
                "SELECT job_id FROM publication_jobs WHERE "
                "(state='pending' OR (state='claimed' AND "
                "lease_expires_at<=?)) AND attempts < max_attempts "
                "ORDER BY created_at LIMIT 1", (now,))
            row = await cur.fetchone()
            if not row:
                return None
            cur = await conn.execute(
                "UPDATE publication_jobs SET state='claimed', "
                "owner_worker_id=?, lease_expires_at=?, attempts=attempts+1, "
                "updated_at=? WHERE job_id=? AND state IN "
                "('pending','claimed') AND (lease_expires_at IS NULL OR "
                "lease_expires_at<=?)",
                (worker_id, now + lease_seconds, now, row["job_id"], now))
            if not cur.rowcount:
                return None
        row = await self.db.fetch_one(
            "SELECT * FROM publication_jobs WHERE job_id=?", (job_id := row["job_id"],))
        return dict(row)

    async def record_publication_progress(self, job_id: str, *,
                                          worker_id: str, stage: str,
                                          state: Optional[str] = None,
                                          error: Optional[str] = None) -> bool:
        now = epoch_now()
        sets = "stage=?, updated_at=?"
        args: List[Any] = [stage, now]
        if state is not None:
            sets += ", state=?"
            args.append(state)
            # Returning to 'pending' clears the lease — a retryable job
            # must be claimable by the NEXT worker immediately.
            if state == "pending":
                sets += ", lease_expires_at=NULL, owner_worker_id=NULL"
        if error is not None:
            sets += ", last_error=?"
            args.append(error)
        args += [job_id, worker_id]
        cur = await self.db.execute(
            f"UPDATE publication_jobs SET {sets} WHERE job_id=? AND "
            "owner_worker_id=? AND state='claimed'", args)
        await self.db.commit()
        return bool(cur.rowcount)

    async def publication_jobs_requiring_recovery(self, *,
                                                  limit: int = 20)             -> List[Dict[str, Any]]:
        """C8: jobs that need the reconciler to EXECUTE recovery —
        failed with attempts left, or expired claims."""
        now = epoch_now()
        rows = await self.db.fetch_all(
            "SELECT * FROM publication_jobs WHERE "
            "(state='pending' AND attempts < max_attempts) OR "
            "(state='failed' AND attempts < max_attempts) OR "
            "(state='claimed' AND lease_expires_at<=?) "
            "ORDER BY created_at LIMIT ?", (now, limit))
        return [dict(r) for r in rows]

    async def recover_publication(self, job: Dict[str, Any], *,
                                  worker_id: str) -> Dict[str, Any]:
        """C8: the reconciler EXECUTES durable recovery — re-drives the
        staged publication from the recorded stage and records the
        truthful outcome. A job exhausted at max attempts parks as
        'failed' permanently visible. The caller may pass an
        ALREADY-CLAIMED job (this worker holds the lease) or a bare
        descriptor — claiming happens only when not already held."""
        claimed = job
        if job.get("state") != "claimed" or                 job.get("owner_worker_id") != worker_id:
            claimed = await self.claim_publication_job(worker_id)
            if not claimed or claimed.get("job_id") != job.get("job_id"):
                return {"recovered": False, "reason": "claim lost"}
        from backend.services.deliverable_publisher import (
            publish_deliverable, validate_manifest)
        d = await self.get(job["deliverable_id"])
        if d is None:
            await self.record_publication_progress(
                job["job_id"], worker_id=worker_id, stage="abandoned",
                state="failed", error="deliverable record missing")
            return {"recovered": False, "reason": "record missing"}
        files = []
        for f in (d.file_manifest or []):
            files.append({**f, "content": f.get("content", "")})
        async def _emit_completion(artifact_id: str, checksum: str):
            """The recovery worker's durable completion record — the
            emission happens HERE, so completion_emitted is truthful
            (§19 C3/C8)."""
            await self.record_publication_progress(
                job["job_id"], worker_id=worker_id,
                stage="completion_event_recorded")

        tx = await publish_deliverable(
            repo=self, deliverable_id=d.deliverable_id, files=files,
            emit_fn=_emit_completion,
            fail_after=claimed.get("fail_after"))
        # publish_deliverable RETURNS injected/real failures instead of
        # raising — the truthful outcome is computed from BOTH paths.
        if tx.failures or tx.stage != "completion_emitted":
            attempts = int(claimed.get("attempts") or 0)
            max_attempts = int(claimed.get("max_attempts") or 5)
            error = (tx.failures[-1]["error"] if tx.failures
                     else f"publication stopped at {tx.stage}")
            final = "failed" if attempts >= max_attempts else "pending"
            await self.record_publication_progress(
                job["job_id"], worker_id=worker_id, stage=tx.stage,
                state=final, error=error[:300])
            return {"recovered": False, "stage": tx.stage,
                    "parked": final}
        await self.record_publication_progress(
            job["job_id"], worker_id=worker_id, stage=tx.stage,
            state="complete")
        return {"recovered": True, "stage": tx.stage,
                "job_id": job["job_id"]}


def content_checksum_from_manifest(files: List[Dict[str, Any]]) -> str:
    """The §17.8 aggregate digest over the manifest entries."""
    import hashlib as _hl
    digest = _hl.sha256()
    for f in sorted(files or [], key=lambda x: x.get("path", "")):
        digest.update(str(f.get("path", "")).encode())
        digest.update(b"\0")
        digest.update(str(f.get("sha256", "")).encode())
        digest.update(b"\0")
        digest.update(str(f.get("byte_length", 0)).encode())
    return digest.hexdigest()[:32]




_repo = None


def set_deliverable_repo(repo) -> None:
    global _repo
    _repo = repo


def get_deliverable_repo():
    return _repo


def reset_deliverable_repo() -> None:
    """§18.1: registered repositories reset on shutdown and between
    tests — a stale accessor never resolves a dead store."""
    global _repo
    _repo = None


def require_deliverable_repo():
    """§18.1: a service advertising deliverable support FAILS READINESS
    clearly when the storage is unavailable — no silent None."""
    if _repo is None:
        from backend.services.specialist_release import SpecialistReleaseError
        raise SpecialistReleaseError(
            "the deliverable store is not configured — deliverable "
            "support cannot start (§18.1)")
    return _repo
