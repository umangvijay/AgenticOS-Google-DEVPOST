"""Outbox dispatcher (Phase 4 §4.8).

Publishes committed-but-unpublished events with a crash-safe lease protocol:

1. Claim a bounded batch (lease expires so another dispatcher recovers
   abandoned claims).
2. Publish using the STABLE event identity.
3. Mark published ONLY after the transport confirms.
4. Failures: bounded exponential backoff via available_at doubling.

The hard boundary — publish succeeds, process dies before the marker —
results in a RE-PUBLISH after recovery; consumers must therefore tolerate
duplicate delivery (§4.9 inbox / idempotent effects). The outbox event stays
small: large outputs belong behind artifact references.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from backend.models.command import OutboxEvent

logger = logging.getLogger(__name__)

RETRY_METRICS: dict[str, int] = {}

PUBLISH = Callable[[OutboxEvent], Awaitable[None]]


def outbox_metrics() -> dict[str, int]:
    return dict(RETRY_METRICS)


async def dispatch_batch(repo, message_bus, dispatcher_owner: str,
                         batch: int = 25, lease_seconds: int = 60) -> int:
    """One dispatcher pass. Returns the number of events published."""
    claimed = await repo.claim_outbox_batch(dispatcher_owner, batch, lease_seconds)
    published = 0
    for event in claimed:
        try:
            topic = _topic_for(event)
            payload = event.payload or {}
            # C1 (§5.0 Batch A): ONE versioned envelope. run_id/task_id are
            # TOP-LEVEL so the worker's existing trigger path can route, and
            # event_type marks the envelope for command-aware consumers.
            await message_bus.publish(topic, {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "command_id": event.command_id,
                "aggregate_id": event.aggregate_id,
                "aggregate_version": event.aggregate_version,
                "schema_version": event.schema_version,
                "run_id": payload.get("run_id"),
                "task_id": payload.get("task_id"),
                "payload": payload,
            })
        except Exception as exc:
            RETRY_METRICS["publish_failed"] = RETRY_METRICS.get("publish_failed", 0) + 1
            await repo.fail_outbox_event(
                event.event_id, type(exc).__name__[:60])
            logger.warning("Outbox publish failed for %s (attempt %d): %s",
                           event.event_id, event.publish_attempts + 1,
                           type(exc).__name__)
            continue
        # §4.8: only after the transport confirms.
        if await repo.mark_outbox_published(event.event_id):
            published += 1
            RETRY_METRICS["published"] = RETRY_METRICS.get("published", 0) + 1
    return published


def _topic_for(event: OutboxEvent) -> str:
    payload = event.payload or {}
    command_type = payload.get("command_type") or ""
    # Workload-class → bus topic mapping (same topics the workers consume).
    return {
        "workflow": "agentos-workflow-events",
        "build": "agentos-workflow-events",
        "webhook": "agentos-workflow-events",
        "schedule": "agentos-scheduler-triggers",
    }.get(command_type, "agentos-workflow-events")


async def run_dispatcher(repo, message_bus, owner: str,
                         max_batches: int = 4, batch: int = 25) -> int:
    """Drain up to max_batches of backlog in one housekeeping pass."""
    total = 0
    await repo.reclaim_stale_outbox_leases()
    for _ in range(max_batches):
        published = await dispatch_batch(repo, message_bus, owner, batch)
        total += published
        if published < batch:
            break
    return total
