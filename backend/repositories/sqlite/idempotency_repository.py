"""
AgentOS — SQLite Idempotency Repository

Execution deduplication ledger using atomic INSERT with unique constraint.
Prevents duplicate tool calls in concurrent/distributed environments.
"""

import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from backend.repositories.base import BaseIdempotencyRepository
from backend.repositories.sqlite.database import DatabaseManager

logger = logging.getLogger(__name__)


class SQLiteIdempotencyRepository(BaseIdempotencyRepository):

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def atomic_insert(self, key: str, workflow_id: str, task_id: str) -> bool:
        """
        Attempts atomic INSERT. Returns True if we own the lock.
        Uses INSERT ... ON CONFLICT DO NOTHING + rowcount check.
        
        This is the core idempotency mechanism:
        - If INSERT succeeds (rowcount=1): we claimed the execution lock
        - If INSERT is ignored (rowcount=0): another worker already has it
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = await self.db.connection()

        cursor = await conn.execute(
            """
            INSERT INTO idempotency_ledger (idempotency_key, workflow_id, task_id, status, created_at, updated_at)
            VALUES (?, ?, ?, 'running', ?, ?)
            ON CONFLICT(idempotency_key) DO UPDATE SET
                status = 'running', updated_at = excluded.updated_at
            WHERE idempotency_ledger.status = 'paused'
            """,
            (key, workflow_id, task_id, now, now),
        )
        await conn.commit()

        success = cursor.rowcount > 0
        if success:
            logger.debug(f"[IDEMPOTENCY] Claimed execution lock: {key[:16]}...")
        else:
            logger.debug(f"[IDEMPOTENCY] Key already exists: {key[:16]}...")
        return success

    async def get_entry(self, key: str) -> Optional[Dict[str, Any]]:
        """Read one ledger entry without claiming it (§29.19 revision)."""
        conn = await self.db.connection()
        cursor = await conn.execute(
            "SELECT workflow_id, task_id, status, result_payload "
            "FROM idempotency_ledger WHERE idempotency_key = ?", (key,))
        row = await cursor.fetchone()
        if not row:
            return None
        return {"workflow_id": row["workflow_id"],
                "task_id": row["task_id"],
                "status": row["status"],
                "result_payload": row["result_payload"]}

    async def release_replay_key(self, key: str) -> bool:
        """§4.16: a delivery's replay identity is NOT permanently consumed
        when command acceptance FAILED (queue full, admission unavailable) —
        releasing it lets the sender's retry be accepted. Only entries that
        never reached a terminal/accepted state are releasable."""
        cursor = await self.db.execute(
            "DELETE FROM idempotency_ledger WHERE idempotency_key = ? AND "
            "status IN ('running', 'paused')", (key,))
        await self.db.commit()
        return bool(cursor.rowcount)

    async def get_record(self, key: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM idempotency_ledger WHERE idempotency_key = ?",
            (key,),
        )
        return dict(row) if row else None

    async def commit_success(self, key: str, result_payload: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            UPDATE idempotency_ledger
            SET status = 'completed', result_payload = ?, updated_at = ?
            WHERE idempotency_key = ?
            """,
            (result_payload, now, key),
        )
        await self.db.commit()
        logger.debug(f"[IDEMPOTENCY] Committed success: {key[:16]}...")

    async def commit_failure(self, key: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            UPDATE idempotency_ledger
            SET status = 'failed', updated_at = ?
            WHERE idempotency_key = ?
            """,
            (now, key),
        )
        await self.db.commit()
        logger.debug(f"[IDEMPOTENCY] Committed failure: {key[:16]}...")

    async def commit_pause(self, key: str) -> None:
        await self.db.execute(
            "UPDATE idempotency_ledger SET status = 'paused' WHERE idempotency_key = ? AND status = 'running'",
            (key,),
        )
        await self.db.commit()

    async def commit_uncertain(self, key: str) -> None:
        await self.db.execute(
            "UPDATE idempotency_ledger SET status = 'uncertain', updated_at = ? WHERE idempotency_key = ? AND status = 'running'",
            (datetime.now(timezone.utc).isoformat(), key),
        )
        await self.db.commit()

    async def sweep_stale_running(self, older_than_seconds: int = 1800) -> int:
        """Quarantine ledger entries stuck in 'running' past the threshold.

        A worker crash between claim and commit leaves 'running' forever.
        We do NOT know whether the external effect happened, so the stale
        entry becomes 'uncertain' (audit F07) — never 'failed', which would
        invite a blind replay of an irreversible action."""
        cutoff = (datetime.now(timezone.utc).isoformat())
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        cursor = await self.db.execute(
            """
            UPDATE idempotency_ledger SET status = 'uncertain', updated_at = ?
            WHERE status = 'running' AND updated_at < ?
            """,
            (datetime.now(timezone.utc).isoformat(), cutoff),
        )
        await self.db.commit()
        return cursor.rowcount or 0

    async def reconcile_uncertain(self, key: str, outcome: str, result_payload: Optional[str] = None) -> None:
        """Deliberate resolution of an uncertain outcome (§5.11): the
        read-validate-write sequence runs on an OWNED connection with a
        CONDITIONAL update (`WHERE status='uncertain'`) inside the same
        transaction — two concurrent resolvers produce exactly one winner;
        the loser raises instead of overwriting history."""
        if outcome not in ("completed", "failed"):
            raise ValueError("outcome must be 'completed' or 'failed'")
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT status FROM idempotency_ledger WHERE idempotency_key = ?",
                (key,),
            )
            row = await cursor.fetchone()
            if not row or row["status"] != "uncertain":
                raise ValueError(
                    "Only 'uncertain' ledger entries can be reconciled")
            now = datetime.now(timezone.utc).isoformat()
            new_status = "completed" if outcome == "completed" else "failed"
            cursor = await conn.execute(
                "UPDATE idempotency_ledger SET status = ?, result_payload = ?, "
                "updated_at = ? WHERE idempotency_key = ? AND status = 'uncertain'",
                (new_status, result_payload or "{}", now, key),
            )
            if not cursor.rowcount:
                raise ValueError(
                    "Concurrent resolution — the entry is no longer uncertain")

    async def reset_failed_task(self, workflow_id: str, task_id: str) -> None:
        # Explicit retries release only failures, never completed effects or live locks.
        await self.db.execute(
            "UPDATE idempotency_ledger SET status = 'paused' WHERE workflow_id = ? AND task_id = ? AND status = 'failed'",
            (workflow_id, task_id),
        )
        await self.db.commit()
