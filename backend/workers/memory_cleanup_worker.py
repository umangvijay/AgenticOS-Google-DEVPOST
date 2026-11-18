"""Memory cleanup worker (Phase 18 C12) — the PRODUCTION caller for the
durable cleanup jobs. Each tick:
1. Claim a cleanup job (lease + attempt count; fencing via owner).
2. Clean each registered dependent (per-dependent status recorded).
3. Record the truthful final state (complete / failed with error).
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

logger = logging.getLogger("memory_cleanup_worker")

WORKER_ID = "memory-cleanup-worker"


async def run_cleanup_pass(repo, *, worker_id: str = WORKER_ID,
                           max_jobs: int = 10) -> int:
    """One pass: claim → clean dependents → record. Returns completions."""
    completed = 0
    claim_owner = f"{worker_id}:{uuid.uuid4().hex}"
    for _ in range(max_jobs):
        job = await repo.claim_cleanup_job(claim_owner)
        if not job:
            break
        errors = []
        for dep in (job.get("dependents") or []):
            try:
                # The dependent cleanup contract: registered kinds map to
                # store deletions. Unknown kinds fail loudly per-dependent.
                kind = dep.get("kind") or dep.get("derived_kind")
                dep_id = dep.get("id") or dep.get("derived_id")
                if kind in ("cached_summary", "correction"):
                    await repo.delete_memory(job["user_id"], dep_id)
                elif kind == "metadata_ref":
                    pass   # a reference only — nothing to clean
                else:
                    errors.append(f"unknown dependent kind: {kind}")
            except Exception as exc:
                errors.append(f"{dep.get('id') or dep.get('derived_id')}: "
                              f"{str(exc)[:120]}")
        state = "complete" if not errors else "failed"
        accepted = await repo.record_cleanup_progress(
            job["job_id"], worker_id=claim_owner, state=state,
            error=("; ".join(errors)[:300] if errors else None))
        if accepted and state == "complete":
            completed += 1
        logger.info("Cleanup job %s: %s (%d dependents)",
                    job["job_id"], state, len(job.get("dependents") or []))
    return completed
