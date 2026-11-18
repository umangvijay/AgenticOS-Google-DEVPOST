"""Deliverable worker (Phase 18 C8) — the production recovery driver
for staged publication. Every tick:
1. Claim durable publication jobs (expired leases included).
2. Re-drive the staged sequence from the recorded stage.
3. Record the truthful outcome (complete / pending / failed-parked).
Also drives memory cleanup jobs (C12) — see memory_cleanup_worker.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("deliverable_worker")

WORKER_ID = "deliverable-worker"


async def run_publication_recovery(repo, *, worker_id: str = WORKER_ID,
                                   limit: int = 10) -> int:
    """One recovery pass over jobs requiring it. Returns the count of
    publications driven to completion."""
    jobs = await repo.publication_jobs_requiring_recovery(limit=limit)
    completed = 0
    for job in jobs:
        outcome = await repo.recover_publication(job, worker_id=worker_id)
        if outcome.get("recovered"):
            completed += 1
        logger.info("Publication job %s: %s", job["job_id"],
                    outcome.get("stage") or outcome.get("reason"))
    return completed
