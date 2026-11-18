"""Runner execution repository (Phase 7 §7.11) — durable lifecycle records.

Persisted per execution: runner assignment, lease owner + expiry, attempt
number, sandbox identity, start/stop times, exit reason, cleanup status, and
output references. Execution state is SEPARATE from cleanup state. A crashed
worker cannot leave an indefinitely running sandbox: the worker ticker
reconciles leases past expiry and marks orphans for cleanup.

Acceptance (§7.11): losing the API connection does not lose the execution
record; a crashed worker cannot leave an indefinitely running sandbox.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

logger = logging.getLogger(__name__)


class RunnerExecutionRepository:
    def __init__(self, db):
        self.db = db

    async def save_execution(self, record: Dict[str, Any]) -> None:
        await self.db.execute(
            """
            INSERT INTO runner_executions
                (execution_id, tenant_id, artifact_digest, runtime_image_digest,
                 policy_profile_id, operation_allowlist_json, state,
                 cleanup_state, runner_assignment, lease_owner,
                 lease_expires_at, attempt, sandbox_identity, started_at,
                 stopped_at, exit_reason, output_references_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(execution_id) DO UPDATE SET
                state=excluded.state, cleanup_state=excluded.cleanup_state,
                runner_assignment=excluded.runner_assignment,
                lease_owner=excluded.lease_owner,
                lease_expires_at=excluded.lease_expires_at,
                attempt=excluded.attempt, sandbox_identity=excluded.sandbox_identity,
                started_at=excluded.started_at, stopped_at=excluded.stopped_at,
                exit_reason=excluded.exit_reason,
                output_references_json=excluded.output_references_json,
                updated_at=excluded.updated_at
            """,
            (record["execution_id"], record.get("tenant_id"),
             record.get("artifact_digest"), record.get("runtime_image_digest"),
             record.get("policy_profile_id"),
             json.dumps(record.get("operation_allowlist") or []),
             record.get("state", "queued"), record.get("cleanup_state", "pending"),
             record.get("runner_assignment"), record.get("lease_owner"),
             record.get("lease_expires_at"), int(record.get("attempt", 1)),
             record.get("sandbox_identity"), record.get("started_at"),
             record.get("stopped_at"), record.get("exit_reason"),
             json.dumps(record.get("output_references") or []), epoch_now()))
        await self.db.commit()

    async def get_execution(self, execution_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM runner_executions WHERE execution_id=?", (execution_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["operation_allowlist"] = json.loads(rec.get("operation_allowlist_json") or "[]")
        rec["output_references"] = json.loads(rec.get("output_references_json") or "[]")
        return rec

    async def executions_for_tenant(self, tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM runner_executions WHERE tenant_id=? "
            "ORDER BY updated_at DESC LIMIT ?", (tenant_id, limit))
        return [dict(r) for r in await cursor.fetchall()]

    async def cancel_pending(self, execution_id: str) -> bool:
        """§7.18 step 1: record cancellation requested; stop admitting new
        calls. Idempotent."""
        cursor = await self.db.execute(
            "UPDATE runner_executions SET state='cancel_requested', updated_at=? "
            "WHERE execution_id=? AND state IN ('queued','starting','running')",
            (epoch_now(), execution_id))
        await self.db.commit()
        return bool(cursor.rowcount)

    async def mark_terminated(self, execution_id: str, exit_reason: str) -> bool:
        cursor = await self.db.execute(
            "UPDATE runner_executions SET state='cancelled', stopped_at=?, "
            "exit_reason=?, updated_at=? WHERE execution_id=? "
            "AND state IN ('cancel_requested','running','starting','queued')",
            (epoch_now(), exit_reason, epoch_now(), execution_id))
        await self.db.commit()
        return bool(cursor.rowcount)

    async def orphaned_executions(self, lease_grace_seconds: int = 120,
                                  limit: int = 50) -> List[Dict[str, Any]]:
        """§7.11 acceptance: a crashed worker cannot leave an indefinitely
        running sandbox — executions whose lease expired while still running
        are orphan candidates for the reconciler."""
        cutoff = epoch_now() - lease_grace_seconds
        cursor = await self.db.execute(
            "SELECT * FROM runner_executions WHERE state IN "
            "('queued','starting','running') AND "
            "(lease_expires_at IS NULL OR lease_expires_at<=?) LIMIT ?",
            (cutoff, limit))
        return [dict(r) for r in await cursor.fetchall()]

    async def pending_cleanup(self, limit: int = 50) -> List[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM runner_executions WHERE cleanup_state='pending' "
            "AND state IN ('completed','failed','timed_out','cancelled','superseded') "
            "LIMIT ?", (limit,))
        return [dict(r) for r in await cursor.fetchall()]

    async def mark_cleaned(self, execution_id: str) -> bool:
        cursor = await self.db.execute(
            "UPDATE runner_executions SET cleanup_state='cleaned', updated_at=? "
            "WHERE execution_id=?", (epoch_now(), execution_id))
        await self.db.commit()
        return bool(cursor.rowcount)


_repo: Optional[RunnerExecutionRepository] = None


def set_runner_execution_repo(repo) -> None:
    global _repo
    _repo = repo


def get_runner_execution_repo() -> Optional[RunnerExecutionRepository]:
    return _repo
