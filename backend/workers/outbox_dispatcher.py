"""Transactional-outbox dispatcher (§21.4).

Cross-process durable delivery: the worker CLAIMS committed outbox
events straight from the shared Postgres ledger (FOR UPDATE SKIP
LOCKED + lease) and handles them with the same idempotent handlers as
the bus consumers. Termination before/after processing is safe: an
unacked lease is reclaimed after expiry (no lost work), and the
command/event idempotency contracts prevent duplicated consequential
effects.

The NATS JetStream transport remains available (adapters + live tests);
in container profiles where the broker's pull path is unavailable, the
dispatcher is the delivery mechanism for the durable platform.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("outbox_dispatcher")


class OutboxDispatcher:
    def __init__(self, command_repo, handler: Callable[[Any, Any], Awaitable[None]],
                 *, owner: str, batch: int = 10, poll_seconds: float = 1.0,
                 lease_seconds: int = 60):
        self._repo = command_repo
        self._handler = handler
        self._owner = owner
        self._batch = batch
        self._poll = poll_seconds
        self._lease = lease_seconds

    async def dispatch_once(self) -> int:
        """Claim + handle one batch. Returns the handled count."""
        try:
            claimed = await self._repo.claim_outbox_batch(
                self._owner, batch=self._batch, lease_seconds=self._lease)
        except Exception:
            logger.warning("outbox claim failed", exc_info=True)
            return 0
        handled = 0
        for event in claimed:
            envelope = {
                "event_type": event.event_type if hasattr(event, "event_type")
                              else event.get("event_type"),
                "command_id": (event.command_id if hasattr(event, "command_id")
                               else event.get("command_id")),
                "payload": (event.payload if hasattr(event, "payload")
                            else event.get("payload")),
                "event_id": (event.event_id if hasattr(event, "event_id")
                             else event.get("event_id")),
            }

            class _Ctx:
                settled = False

                async def ack(self_inner):
                    self_inner.settled = True
                    await self._repo.mark_outbox_published(envelope["event_id"])

                async def nack(self_inner):
                    # The lease expires and the reclaim loop re-delivers —
                    # bounded by publish_attempts accounting.
                    await self._repo.fail_outbox_event(
                        envelope["event_id"], "handler_nacked")

            try:
                await self._handler(envelope, _Ctx())
                handled += 1
            except Exception:
                logger.warning("outbox handler failed for %s",
                               str(envelope.get("event_id"))[:40],
                               exc_info=True)
                try:
                    await self._repo.fail_outbox_event(
                        envelope["event_id"], "handler_error")
                except Exception:
                    pass
        return handled

    async def run(self, *, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            handled = await self.dispatch_once()
            # Reclaim stale leases (workers that died mid-handling).
            try:
                reclaimed = await self._repo.reclaim_stale_outbox_leases()
                if reclaimed:
                    logger.info("reclaimed %d stale outbox lease(s)", reclaimed)
            except Exception:
                logger.debug("outbox reclaim failed", exc_info=True)
            await asyncio.sleep(0.05 if handled else self._poll)


class DirectTaskRunner:
    """DB-driven task execution loop (§21.2/§21.4): claims READY pending
    tasks (dependencies satisfied) with the durable lease and executes
    them through the engine. The lease + conditional updates make
    concurrent workers safe; a killed worker's tasks are reclaimable
    after the lease expires (§20.18 semantics)."""

    def __init__(self, repo, process_task, *, owner: str, poll_seconds: float = 1.0,
                 lease_seconds: int = 120, limit: int = 5):
        self._repo = repo
        self._process_task = process_task
        self._owner = owner
        self._poll = poll_seconds
        self._lease = lease_seconds
        self._limit = limit

    async def _ready_tasks(self) -> list:
        """Pending tasks whose dependencies are all satisfied. The
        dependency check runs in Python (task payloads carry deps in
        heterogeneous shapes); the LEASE claim arbitrates concurrent
        workers."""
        rows = await self._repo.db.fetch_all(
            """SELECT t.run_id, t.id AS task_id, t.payload
               FROM tasks t
               WHERE t.status = 'PENDING'
                  OR (t.status = 'RUNNING' AND
                      (t.lease_expires_at IS NULL OR t.lease_expires_at < now()))
               ORDER BY t.created_at LIMIT 50""")
        by_run: dict = {}
        for r in rows:
            by_run.setdefault(r["run_id"], []).append(r)
        ready = []
        for run_id, tasks in by_run.items():
            siblings = await self._repo.db.fetch_all(
                "SELECT id, status FROM tasks WHERE run_id=$1", run_id)
            state = {x["id"]: x["status"] for x in siblings}
            for t in tasks:
                deps = _parse_deps(t.get("payload"))
                if all(state.get(d) in ("COMPLETED", "SKIPPED")
                       for d in deps):
                    ready.append({"run_id": run_id, "task_id": t["task_id"]})
                if len(ready) >= self._limit:
                    return ready
        return ready

    async def run(self, *, stop: Optional[asyncio.Event] = None) -> None:
        while stop is None or not stop.is_set():
            try:
                ready = await self._ready_tasks()
                for row in ready:
                    run_id, task_id = row["run_id"], row["task_id"]
                    # NO pre-claim: the engine's OWN CAS claim arbitrates
                    # concurrent workers (claim_task sets RUNNING).
                    logger.info("direct executor: task %s/%s dispatched",
                                run_id[:12], task_id)
                    await self._process_task(run_id, task_id)
            except Exception:
                logger.warning("direct task runner iteration failed",
                               exc_info=True)
                await asyncio.sleep(2)
            await asyncio.sleep(self._poll)


def _parse_deps(payload) -> list:
    if not payload:
        return []
    if isinstance(payload, str):
        import json as _json
        try:
            payload = _json.loads(payload)
        except ValueError:
            return []
    deps = payload.get("dependencies") or []
    if isinstance(deps, str):
        import json as _json
        try:
            deps = _json.loads(deps)
        except ValueError:
            deps = [d for d in deps.split(",") if d]
    return list(deps or [])
