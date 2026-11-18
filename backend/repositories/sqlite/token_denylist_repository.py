"""
AgentOS — SQLite Token Denylist Repository

Server-side access-token revocation. Logout writes the token's jti with
its natural expiry; the auth dependency rejects any listed jti. Entries
self-expire, so the table stays small.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from backend.repositories.base import BaseTokenDenylistRepository
from backend.repositories.sqlite.database import DatabaseManager

logger = logging.getLogger(__name__)


class SQLiteTokenDenylistRepository(BaseTokenDenylistRepository):

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def revoke_jti(self, jti: str, expires_at: datetime) -> None:
        if not jti:
            return
        now = datetime.now(timezone.utc).isoformat()
        expiry = expires_at if isinstance(expires_at, str) else expires_at.isoformat()
        await self.db.execute(
            """
            INSERT INTO token_denylist (jti, expires_at, revoked_at)
            VALUES (?, ?, ?)
            ON CONFLICT(jti) DO NOTHING
            """,
            (jti, expiry, now),
        )
        await self.db.commit()

    async def is_revoked(self, jti: str) -> bool:
        if not jti:
            return False
        row = await self.db.fetch_one(
            "SELECT 1 FROM token_denylist WHERE jti = ? AND expires_at > ?",
            (jti, datetime.now(timezone.utc).isoformat()),
        )
        return row is not None

    async def purge_expired(self) -> int:
        conn = await self.db.connection()
        cursor = await conn.execute(
            "DELETE FROM token_denylist WHERE expires_at <= ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await conn.commit()
        return cursor.rowcount or 0
