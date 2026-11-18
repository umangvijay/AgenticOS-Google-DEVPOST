"""Shared runner journal with a private database loop for supervisor threads.

The synchronous supervisor contract never borrows the application's asyncio
connections. Cancellation is shared across replicas; orphan cleanup is restricted
to the Docker engine that actually owns the execution namespace.
"""
import asyncio
import json
import re
import sqlite3
import threading
import time

import asyncpg


class PostgresExecutionStore:
    def __init__(self, dsn, *, schema="public"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
            raise ValueError("Invalid runner database schema")
        self.host_id = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="runner-journal", daemon=True)
        self.thread.start()
        self.closed = False
        async def connect():
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, command_timeout=5,
                                            server_settings={"search_path": schema + ",public"})
            try:
                await pool.fetchval("SELECT COUNT(*) FROM managed_executions WHERE false")
            except Exception:
                await pool.close()
                raise
            return pool
        try:
            self.pool = self._wait(connect())
        except Exception:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=3)
            self.loop.close()
            raise

    def _wait(self, operation):
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        try:
            return future.result(timeout=10)
        except Exception:
            future.cancel()
            raise

    def _query(self, query, *args, many=False):
        async def execute():
            return await (self.pool.fetch(query, *args) if many else self.pool.fetchrow(query, *args))
        return self._wait(execute())

    def bind_host(self, host_id):
        if not host_id or self.host_id and self.host_id != host_id:
            raise ValueError("Runner journal cannot change its host binding")
        self.host_id = host_id

    def admit(self, execution_id, request, deadline):
        if not self.host_id:
            raise ValueError("Runner journal needs a stable runtime host identity")
        try:
            self._query("INSERT INTO managed_executions(id,runner_host_id,request,state,cleanup,deadline) "
                        "VALUES($1,$2,$3::jsonb,'queued','pending',$4) RETURNING id",
                        execution_id, self.host_id, json.dumps(request), deadline)
        except asyncpg.UniqueViolationError as exc:
            raise sqlite3.IntegrityError("Execution already admitted") from exc

    def transition(self, execution_id, before, after):
        # Cancellation is intentionally allowed from another replica. Starting
        # execution is only allowed on the host that owns the admitted work.
        row = self._query("UPDATE managed_executions SET state=$3 WHERE id=$1 AND state=ANY($2::text[]) "
                          "AND ($3 != 'running' OR runner_host_id=$4) RETURNING id",
                          execution_id, before, after, self.host_id)
        return row is not None

    def finish(self, execution_id, state, cleanup, reason):
        self._query("UPDATE managed_executions SET "
                    "state=CASE WHEN state='cancel_requested' AND $2='completed' THEN 'cancelled' ELSE $2 END, "
                    "reason=CASE WHEN state='cancel_requested' AND $2='completed' "
                    "THEN 'cancelled before result commit' ELSE $4 END, cleanup=$3,stopped=$5 "
                    "WHERE id=$1 AND runner_host_id=$6 AND state IN ('queued','running','cancel_requested') RETURNING id",
                    execution_id, state, cleanup, reason, time.time(), self.host_id)

    def cancel_queued(self, execution_id):
        return self._query("UPDATE managed_executions SET state='cancelled',cleanup='cleaned',stopped=$2,"
                           "reason='cancelled before launch' WHERE id=$1 AND state='queued' RETURNING id",
                           execution_id, time.time()) is not None

    def put(self, execution_id, result):
        row = self._query("UPDATE managed_executions SET result=$2::jsonb WHERE id=$1 AND runner_host_id=$3 "
                          "AND state IN ('queued','running','cancel_requested') RETURNING id",
                          execution_id, json.dumps(result), self.host_id)
        if row is None:
            raise ValueError("Execution is missing, terminal, or belongs to another runtime host")

    def get(self, execution_id):
        row = self._query("SELECT * FROM managed_executions WHERE id=$1", execution_id)
        return dict(row) if row else None

    def cancellation_requested(self, execution_id):
        row = self.get(execution_id)
        return row is not None and row["state"] == "cancel_requested"

    def expired(self):
        return [row["id"] for row in self._query("SELECT id FROM managed_executions "
            "WHERE runner_host_id=$1 AND deadline<$2 AND (state IN ('queued','running','cancel_requested') "
            "OR cleanup='orphaned')", self.host_id, time.time(), many=True)]

    def cleaned(self, execution_id):
        self._query("UPDATE managed_executions SET cleanup='cleaned' WHERE id=$1 AND runner_host_id=$2 RETURNING id",
                    execution_id, self.host_id)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._wait(self.pool.close())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=3)
            self.loop.close()
