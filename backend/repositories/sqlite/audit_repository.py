"""
AgentOS — SQLite Audit Repository

Append-only, structured audit log.
Never logs secrets, bearer tokens, or API keys.
"""

import json
import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta, timezone

from backend.repositories.base import BaseAuditRepository
from backend.repositories.sqlite.database import DatabaseManager

logger = logging.getLogger(__name__)


class SQLiteAuditRepository(BaseAuditRepository):

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def log_event(self, event_data: Dict[str, Any]) -> None:
        e = event_data
        timestamp = e.get("timestamp", datetime.now(timezone.utc).isoformat())
        
        await self.db.execute(
            """
            INSERT INTO audit_logs (
                event_type, actor_id, actor_type, resource_id,
                workflow_id, run_id, task_id, trace_id,
                details, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                e["event_type"],
                e["actor_id"],
                e["actor_type"],
                e["resource_id"],
                e.get("workflow_id"),
                e.get("run_id"),
                e.get("task_id"),
                e.get("trace_id"),
                json.dumps(e.get("details", {})),
                timestamp,
            ),
        )
        await self.db.commit()

    async def get_events(self, resource_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """
            SELECT * FROM audit_logs
            WHERE resource_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (resource_id, limit),
        )
        results = []
        for row in rows:
            r = dict(row)
            r["details"] = json.loads(r.get("details", "{}"))
            results.append(r)
        return results

    async def list_recent(self, limit: int = 100, offset: int = 0, event_type: str = None) -> List[Dict[str, Any]]:
        """Admin listing: most recent audit events across all resources."""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        if event_type:
            rows = await self.db.fetch_all(
                """
                SELECT * FROM audit_logs
                WHERE event_type = ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (event_type, limit, offset),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        results = []
        for row in rows:
            r = dict(row)
            r["details"] = json.loads(r.get("details", "{}"))
            results.append(r)
        return results

    async def purge_older_than(self, days: int) -> int:
        """Retention control: delete audit rows older than N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        conn = await self.db.connection()
        cursor = await conn.execute("DELETE FROM audit_logs WHERE timestamp < ?", (cutoff,))
        await conn.commit()
        return cursor.rowcount or 0
