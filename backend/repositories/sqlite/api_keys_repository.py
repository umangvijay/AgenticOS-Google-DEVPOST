"""
AgentOS — API Keys Repository

Per-user programmatic access keys (prefix `vry_`). Only the SHA-256 hash
is stored; the raw key is shown once at creation. Keys authenticate the
same as JWTs (same role, same scoping) and are revocable individually —
the missing middle ground between 15-minute JWTs and "logout everywhere".
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.repositories.base import BaseApiKeysRepository
from backend.repositories.sqlite.database import DatabaseManager

logger = logging.getLogger(__name__)


class SQLiteApiKeysRepository(BaseApiKeysRepository):

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def create_key(self, user_id: str, key_hash: str, prefix: str, name: str) -> str:
        key_id = key_hash[:16]
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            INSERT INTO api_keys (id, user_id, name, key_hash, prefix, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key_id, user_id, name, key_hash, prefix, now),
        )
        await self.db.commit()
        return key_id

    async def resolve_key(self, key_hash: str) -> Optional[Dict[str, Any]]:
        """Return {user_id, key_id} for a live (non-revoked) key, updating last_used."""
        row = await self.db.fetch_one(
            """
            SELECT id, user_id, revoked_at FROM api_keys
            WHERE key_hash = ? AND revoked_at IS NULL
            """,
            (key_hash,),
        )
        if not row:
            return None
        await self.db.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), row["id"]),
        )
        await self.db.commit()
        return {"user_id": row["user_id"], "key_id": row["id"]}

    async def list_keys(self, user_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """
            SELECT id, name, prefix, created_at, last_used_at, revoked_at
            FROM api_keys WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def revoke_key(self, user_id: str, key_id: str) -> bool:
        cursor = await self.db.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), key_id, user_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0
