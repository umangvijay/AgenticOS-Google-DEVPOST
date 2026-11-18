"""asyncpg pool + explicit versioned migration runner (§20.4).

Migrations are SQL files in ``migrations/`` named ``NNNN_label.sql`` and
applied in lexical order exactly once, recorded in ``schema_migrations``
(name, checksum). A checksum mismatch fails loudly — a changed migration
must be a NEW file, never an edit of an applied one.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncpg

logger = logging.getLogger("postgres_database")

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME_RE = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    pass


class PostgresDatabaseManager:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 5,
                 schema: Optional[str] = None):
        self._dsn = dsn
        self._min = min_size
        self._max = max_size
        # A dedicated schema isolates a deployment/tenant/test run: the
        # search_path is enforced in server_settings so EVERY pooled
        # connection uses it (per-connection SET would not survive
        # acquire() handing out other connections).
        self._schema = schema
        if schema is not None and not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", schema):
            raise ValueError("Postgres schema must be a simple identifier of at most 63 characters")
        self._pool: Optional[asyncpg.Pool] = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Postgres pool not initialized — call "
                               "initialize() first")
        return self._pool

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        server_settings = None
        if self._schema:
            boot = await asyncpg.connect(self._dsn)
            try:
                async with boot.transaction():
                    await boot.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 1701))", self._schema)
                    await boot.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
            finally:
                await boot.close()
            server_settings = {"search_path": f"{self._schema},public"}
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min, max_size=self._max,
            server_settings=server_settings)
        await self.apply_migrations()

    async def close(self, *, drop_schema: bool = False) -> None:
        if self._pool is not None:
            if drop_schema and self._schema:
                await self._pool.execute(
                    f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
            await self._pool.close()
            self._pool = None

    # ── migrations ────────────────────────────────────────────────────

    @staticmethod
    def _migration_files() -> List[Path]:
        files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
        for f in files:
            if not _NAME_RE.match(f.name):
                raise MigrationError(
                    f"migration filename {f.name!r} does not match "
                    "NNNN_label.sql — refusing ambiguous ordering")
        return files

    async def apply_migrations(self) -> List[str]:
        """Apply each unapplied migration in order inside its own
        transaction; record name + checksum. Idempotent across restarts."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Serialize concurrent API/worker startup before either reads the ledger.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), 1701)")
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        name TEXT PRIMARY KEY,
                        checksum TEXT NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now())
                """)
                applied = {r["name"]: r["checksum"]
                           for r in await conn.fetch(
                               "SELECT name, checksum FROM schema_migrations")}
                done: List[str] = []
                for path in self._migration_files():
                    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                    if path.name in applied:
                        if applied[path.name] != checksum:
                            raise MigrationError(
                                f"migration {path.name} was already applied "
                                "with a different checksum — edits to applied "
                                "migrations are forbidden; add a new file")
                        continue
                    async with conn.transaction():
                        await conn.execute(path.read_text())
                        await conn.execute(
                            "INSERT INTO schema_migrations (name, checksum) "
                            "VALUES ($1, $2)", path.name, checksum)
                    logger.info("applied migration %s", path.name)
                    done.append(path.name)
                return done

    async def applied_migrations(self) -> List[Dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT name, checksum, applied_at FROM schema_migrations "
            "ORDER BY name")
        return [dict(r) for r in rows]

    # ── helpers shared by the repositories ────────────────────────────

    async def fetch_one(self, query: str, *args) -> Optional[Dict[str, Any]]:
        row = await self.pool.fetchrow(query, *args)
        return dict(row) if row else None

    async def fetch_all(self, query: str, *args) -> List[Dict[str, Any]]:
        return [dict(r) for r in await self.pool.fetch(query, *args)]

    async def execute(self, query: str, *args) -> str:
        return await self.pool.execute(query, *args)
