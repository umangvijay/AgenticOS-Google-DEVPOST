"""Durable evaluation jobs (Phase 12 §12.11 — the remaining piece).

Terminal events enqueue evaluation work DURABLY: a job row commits with
idempotency on the subject identity, so worker crashes and repeated
terminal events neither lose evaluations nor inflate sample counts. A
reconciler drains pending jobs; duplicate delivery returns the SAME job.

Contract:
- enqueue(run_id) — create-or-get by (run_id, evaluator_version); the
  SAME job comes back for redelivery.
- claim(worker) — CAS lease (one worker per job), deadline-aware.
- complete / fail — attempts increment; failed jobs requeue bounded
  times then park as `exhausted` (visible, never silently dropped).
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now


class EvaluationJobError(Exception):
    def __init__(self, message: str, job_id: str = ""):
        super().__init__(message)
        self.job_id = job_id


def new_job_id() -> str:
    return f"evj_{uuid.uuid4().hex[:20]}"


class EvaluationJobRepository:
    """SQLite reference (Firestore parity follows the established
    anchor pattern)."""

    def __init__(self, db):
        self.db = db

    async def enqueue(self, run_id: str, *,
                      evaluator_version: str = "3.0.0",
                      reason: str = "run_terminal") -> Dict[str, Any]:
        """Create-or-get by (run_id, evaluator_version) — duplicate
        terminal events return the SAME job (§12.11 acceptance)."""
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT * FROM evaluation_jobs WHERE run_id=? AND "
            "evaluator_version=?", (run_id, evaluator_version))
        row = await cursor.fetchone()
        if row:
            return dict(row)
        job_id = new_job_id()
        await self.db.execute(
            """INSERT INTO evaluation_jobs
               (job_id, run_id, evaluator_version, reason, state,
                attempts, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (job_id, run_id, evaluator_version, reason, now, now))
        await self.db.commit()
        cursor = await self.db.execute(
            "SELECT * FROM evaluation_jobs WHERE job_id=?", (job_id,))
        return dict(await cursor.fetchone())

    async def claim(self, worker_id: str, *,
                    lease_seconds: int = 120,
                    batch: int = 5) -> List[Dict[str, Any]]:
        """CAS claim of pending jobs — one owner per job per lease."""
        now = epoch_now()
        claimed = []
        cursor = await self.db.execute(
            "SELECT job_id, state_version FROM evaluation_jobs "
            "WHERE state='pending' OR (state='leased' AND lease_expires_at<=?) "
            "ORDER BY created_at LIMIT ?", (now, batch))
        rows = await cursor.fetchall()
        for row in rows:
            cursor = await self.db.execute(
                "UPDATE evaluation_jobs SET state='leased', owner_worker_id=?, "
                "lease_expires_at=?, state_version=state_version+1, "
                "updated_at=? WHERE job_id=? AND state_version=? AND "
                "(state='pending' OR lease_expires_at<=?)",
                (worker_id, now + lease_seconds, now, row["job_id"],
                 row["state_version"], now))
            if cursor.rowcount:
                claimed.append(row["job_id"])
        await self.db.commit()
        jobs = []
        for job_id in claimed:
            cursor = await self.db.execute(
                "SELECT * FROM evaluation_jobs WHERE job_id=?", (job_id,))
            jobs.append(dict(await cursor.fetchone()))
        return jobs

    async def claim_one(self, job_id: str, worker_id: str, *,
                        lease_seconds: int = 120) -> Optional[Dict[str, Any]]:
        """§13.2: claim ONE specific job (the terminal-run hook claims its
        OWN run's job — an unrelated older job can never be stranded by
        this invocation)."""
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT state_version, state, lease_expires_at FROM "
            "evaluation_jobs WHERE job_id=?", (job_id,))
        row = await cursor.fetchone()
        if not row or row["state"] not in ("pending", "leased"):
            return None
        if row["state"] == "leased" and \
                (row["lease_expires_at"] or 0) > now:
            return None   # another live lease
        cursor = await self.db.execute(
            "UPDATE evaluation_jobs SET state='leased', owner_worker_id=?, "
            "lease_expires_at=?, state_version=state_version+1, updated_at=? "
            "WHERE job_id=? AND state_version=?",
            (worker_id, now + lease_seconds, now, job_id, row["state_version"]))
        await self.db.commit()
        if not cursor.rowcount:
            return None
        cursor = await self.db.execute(
            "SELECT * FROM evaluation_jobs WHERE job_id=?", (job_id,))
        return dict(await cursor.fetchone())

    async def drain(self, worker_id: str, *, batch: int = 5,
                    run_evaluation=None) -> int:
        """§13.2: the independent bounded consumer — process whichever
        jobs THIS worker claims, for whichever runs they belong to.
        `run_evaluation(job) -> evaluation_id` is the evaluation callable
        (injected by the worker); a missing one just claims nothing."""
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
                try:
                    await self.fail(job["job_id"], str(exc),
                                    owner_worker_id=worker_id)
                except EvaluationJobError:
                    pass   # the lease moved — claim takeover recovers it
        return processed

    async def complete(self, job_id: str, *, evaluation_id: str,
                       owner_worker_id: str) -> None:
        """§13.2/§14.G: completion is FENCED and the claim identity is
        MANDATORY — the caller must name the owner it claimed as, and
        the state transition is a single CAS (owner + live lease + state
        version). A stale worker whose lease expired or was taken over
        cannot overwrite the result, and an owner-less call is not a
        bypass — it is a type error in the contract."""
        if not owner_worker_id:
            raise EvaluationJobError(
                "owner_worker_id is required to complete a job", job_id)
        now = epoch_now()
        conn = await self.db.connection()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                "SELECT owner_worker_id, state_version, state, "
                "lease_expires_at FROM evaluation_jobs WHERE job_id=?",
                (job_id,))
            row = await cursor.fetchone()
            if not row:
                await conn.rollback()
                raise EvaluationJobError("Job not found", job_id)
            if (row["state"] != "leased"
                    or row["owner_worker_id"] != owner_worker_id
                    or (row["lease_expires_at"] or 0) <= now):
                await conn.rollback()
                raise EvaluationJobError(
                    "Stale worker: the lease is no longer yours", job_id)
            cursor = await conn.execute(
                "UPDATE evaluation_jobs SET state='done', evaluation_id=?, "
                "lease_expires_at=NULL, updated_at=? WHERE job_id=? AND "
                "state_version=? AND owner_worker_id=? AND state='leased'",
                (evaluation_id, now, job_id, row["state_version"],
                 owner_worker_id))
            if not cursor.rowcount:
                await conn.rollback()
                raise EvaluationJobError(
                    "Stale worker: the lease moved mid-completion", job_id)
            await conn.commit()
        except EvaluationJobError:
            raise
        except Exception:
            await conn.rollback()
            raise

    async def fail(self, job_id: str, error: str, *,
                   owner_worker_id: str,
                   max_attempts: int = 5) -> Dict[str, Any]:
        """§14.G: failure is FENCED the same way — only the CURRENT
        lease owner can fail the job. Failed jobs requeue with attempts;
        at the cap they park as `exhausted` (visible in health, never
        silently dropped). An expired lease recovers through claim
        takeover, not through an owner-less fail."""
        if not owner_worker_id:
            raise EvaluationJobError(
                "owner_worker_id is required to fail a job", job_id)
        now = epoch_now()
        conn = await self.db.connection()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                "SELECT owner_worker_id, state_version, state, "
                "lease_expires_at FROM evaluation_jobs WHERE job_id=?",
                (job_id,))
            row = await cursor.fetchone()
            if not row:
                await conn.rollback()
                raise EvaluationJobError("Job not found", job_id)
            state_now = row["state"]
            if state_now == "leased":
                # The ownership fence: only the CURRENT live lease owner
                # can fail a leased job.
                if (row["owner_worker_id"] != owner_worker_id
                        or (row["lease_expires_at"] or 0) <= now):
                    await conn.rollback()
                    raise EvaluationJobError(
                        "Stale worker: the lease is no longer yours",
                        job_id)
            elif state_now not in ("pending", "exhausted"):
                # A finished job (done) is never re-failed into requeue.
                await conn.rollback()
                raise EvaluationJobError(
                    "Stale worker: the job is not active", job_id)
            cursor = await conn.execute(
                "UPDATE evaluation_jobs SET attempts=attempts+1, "
                "last_error=?, updated_at=? WHERE job_id=? AND "
                "state_version=?",
                (error[:300], now, job_id, row["state_version"]))
            if not cursor.rowcount:
                await conn.rollback()
                raise EvaluationJobError(
                    "Stale worker: the job moved mid-fail", job_id)
            cursor = await conn.execute(
                "SELECT attempts FROM evaluation_jobs WHERE job_id=?",
                (job_id,))
            row = await cursor.fetchone()
            attempts = int(row["attempts"] or 0)
            state = "exhausted" if attempts >= max_attempts else "pending"
            await conn.execute(
                "UPDATE evaluation_jobs SET state=?, lease_expires_at=NULL, "
                "owner_worker_id=NULL WHERE job_id=?",
                (state, job_id))
            await conn.commit()
        except EvaluationJobError:
            raise
        except Exception:
            await conn.rollback()
            raise
        cursor = await conn.execute(
            "SELECT * FROM evaluation_jobs WHERE job_id=?", (job_id,))
        return dict(await cursor.fetchone())

    async def fail_active_for_run(self, run_id: str, error: str, *,
                                  owner_worker_id: Optional[str] = None,
                                  max_attempts: int = 5) -> None:
        """Record a failure against the run's ACTIVE job. Fenced: a LEASED
        job is failed only by its current owner (the claimer that raised);
        a job with no live owner (pending/expired) may be requeued by the
        reconciler without one."""
        cursor = await self.db.execute(
            "SELECT job_id, state, owner_worker_id FROM evaluation_jobs "
            "WHERE run_id=? AND state IN ('leased','pending') AND "
            "attempts < ?", (run_id, max_attempts))
        row = await cursor.fetchone()
        if not row:
            return
        if row["state"] == "leased":
            if not owner_worker_id or                     row["owner_worker_id"] != owner_worker_id:
                return   # not the owner — the fence holds
        await self.fail(row["job_id"], error,
                        owner_worker_id=owner_worker_id or "reconciler",
                        max_attempts=max_attempts)

    async def reset_exhausted(self) -> int:
        """Operator action: requeue exhausted jobs (they were parked
        visibly, never dropped)."""
        cursor = await self.db.execute(
            "UPDATE evaluation_jobs SET state='pending', attempts=0, "
            "updated_at=? WHERE state='exhausted'", (epoch_now(),))
        await self.db.commit()
        return cursor.rowcount or 0

    async def enqueue_unfinished_terminals(self, run_ids: List[str]) -> int:
        """§13.2 reconciliation: terminal runs discovered without a job
        (the crash window between terminal persistence and enqueue) get
        one — create-or-get semantics make this idempotent."""
        n = 0
        for run_id in run_ids or []:
            existing = await self.db.execute(
                "SELECT job_id FROM evaluation_jobs WHERE run_id=?",
                (run_id,))
            if not await existing.fetchone():
                await self.enqueue(run_id, reason="reconciliation")
                n += 1
        return n

    async def health(self) -> Dict[str, Any]:
        cursor = await self.db.execute(
            "SELECT state, COUNT(*) AS n FROM evaluation_jobs "
            "GROUP BY state")
        by_state = {r["state"]: r["n"] for r in await cursor.fetchall()}
        return {
            "by_state": by_state,
            "pending": by_state.get("pending", 0),
            "exhausted": by_state.get("exhausted", 0),
            "done": by_state.get("done", 0),
        }


_repo: Optional[EvaluationJobRepository] = None


def set_evaluation_job_repo(repo) -> None:
    global _repo
    _repo = repo


def get_evaluation_job_repo() -> Optional[EvaluationJobRepository]:
    return _repo
