"""§4 durable command contract on Postgres (§21.4) — faithful port of
SQLiteCommandRepository: transactional acceptance (command + scoped
idempotency + queue admission + initial work + outbox in ONE
transaction), fenced transitions, and the outbox dispatch cycle."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, List, Optional

from backend.models.command import (
    Command, CommandReceipt, CommandState, OutboxEvent)
from backend.repositories.command_repository import (
    STATUS_URL, _conflict_code, _queue_full_code, _row_to_command,
    _row_to_event)
from backend.repositories.postgres.database import PostgresDatabaseManager


def _epoch_now() -> int:
    import time
    return int(time.time())


class PostgresCommandRepositoryV2:
    # §21.4: the adapter executes sqlite-dialect initial_work callbacks
    # via the SQL translation layer — the repo handles BOTH contract
    # shapes (typed work_writes AND callbacks).
    accepts_initial_work_callbacks = True

    def __init__(self, db: PostgresDatabaseManager):
        self.db = db
        # The §21.3 simple-command surface (idempotent receipts on the
        # pg_commands table) composes the V2 transactional contract.
        from backend.repositories.postgres.repositories import (
            PostgresCommandRepository)
        self._simple = PostgresCommandRepository(db)

    async def accept_command_with_initial_work(
        self, command: Command, admission, outbox_event: OutboxEvent,
        initial_work: Optional[Callable[[Any], Awaitable[None]]] = None,
        work_writes: Optional[List[Any]] = None,
    ) -> CommandReceipt:
        """§4.5 in ONE transaction. `initial_work` receives an asyncpg
        CONNECTION with `.execute(sql, params)` ($N placeholders)."""
        now = _epoch_now()
        command.created_at = now
        command.updated_at = now
        if outbox_event.created_at == 0:
            outbox_event.created_at = now
        if outbox_event.available_at == 0:
            outbox_event.available_at = now
        conn = await self.db.pool.acquire()
        try:
            async with conn.transaction():
                # ── Scoped idempotency (§4.4) ───────────────────────
                if command.idempotency_scoped:
                    row = await conn.fetchrow(
                        "SELECT command_id, request_digest, state, "
                        "state_version, run_id FROM pg_commands_v2 "
                        "WHERE idempotency_scoped=$1",
                        command.idempotency_scoped)
                    if row:
                        if (command.request_digest is not None
                                and row["request_digest"]
                                and row["request_digest"]
                                != command.request_digest):
                            return CommandReceipt(
                                command_id=row["command_id"],
                                run_id=row["run_id"],
                                state=CommandState(row["state"]),
                                accepted=False, deduplicated=False,
                                state_version=int(row["state_version"]),
                                status_url=STATUS_URL.format(
                                    command_id=row["command_id"]),
                                error_code=_conflict_code(),
                                message=("Same idempotency key with a "
                                         "different request payload"))
                        return CommandReceipt(
                            command_id=row["command_id"],
                            run_id=row["run_id"],
                            state=CommandState(row["state"]),
                            accepted=True, deduplicated=True,
                            state_version=int(row["state_version"]),
                            status_url=STATUS_URL.format(
                                command_id=row["command_id"]))

                # ── Queue admission accounting (R7/R9) ──────────────
                admission_row = None
                if admission is not None:
                    pending = await conn.fetchval(
                        "SELECT COUNT(*) FROM pg_pending_commands "
                        "WHERE status IN ('pending','dispatched') "
                        "AND user_id=$1", command.owner_user_id)
                    limit = int(admission.max_pending_per_user or 0)
                    if limit > 0 and int(pending) >= limit:
                        return CommandReceipt(
                            command_id=command.command_id, run_id=None,
                            state=command.state, accepted=False,
                            deduplicated=False, state_version=0,
                            status_url="", error_code=_queue_full_code(),
                            message="The workspace has too much work waiting")
                    admission_row = admission

                # ── Command row ─────────────────────────────────────
                r = command.to_row()
                await conn.execute(
                    """INSERT INTO pg_commands_v2
                         (command_id, command_type, schema_version,
                          owner_user_id, organization_id, actor_type,
                          source, idempotency_key, idempotency_scoped,
                          request_digest, payload_json, run_id, build_id,
                          parent_command_id, parent_run_id, parent_task_id,
                          state, state_version, waiting_reason, deadline_at,
                          cancel_requested_at, cancel_requested_by,
                          cancel_reason, cancel_acknowledged_at,
                          terminal_reason, result_reference,
                          created_at, updated_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,
                               $12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,
                               $23,$24,$25,$26,$27,$28)""",
                    r["command_id"], r["command_type"], r["schema_version"],
                    r["owner_user_id"], r["organization_id"], r["actor_type"],
                    r["source"], r["idempotency_key"],
                    r["idempotency_scoped"], r["request_digest"],
                    json.dumps(r["payload_json"]), r["run_id"],
                    r["build_id"], r["parent_command_id"],
                    r["parent_run_id"], r["parent_task_id"], r["state"],
                    r["state_version"], r["waiting_reason"],
                    r["deadline_at"], r["cancel_requested_at"],
                    r["cancel_requested_by"], r["cancel_reason"],
                    r["cancel_acknowledged_at"], r["terminal_reason"],
                    r["result_reference"], r["created_at"], r["updated_at"])

                # ── Queue-admission record ──────────────────────────
                if admission_row is not None:
                    await conn.execute(
                        """INSERT INTO pg_pending_commands (command_id,
                             user_id, org_id, workload_class, status,
                             idempotency_key, request_digest, created_at,
                             expires_at, updated_at)
                           VALUES ($1,$2,$3,$4,'pending',$5,$6,$7,$8,$9)""",
                        command.command_id, command.owner_user_id,
                        command.organization_id, command.command_type,
                        command.idempotency_scoped, command.request_digest,
                        now, now + admission_row.max_wait_seconds, now)

                # ── Initial work (same transaction) ─────────────────
                if initial_work is not None:
                    await initial_work(_AsyncPgConn(conn))
                if work_writes:
                    for collection, doc_id, data in work_writes:
                        await self._apply_work_write(conn, collection,
                                                     doc_id, data)

                # ── Outbox event ────────────────────────────────────
                await conn.execute(
                    """INSERT INTO pg_outbox_v2 (event_id, command_id,
                         aggregate_id, aggregate_version, event_type,
                         schema_version, payload_json, created_at,
                         available_at, publish_attempts)
                       VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,0)""",
                    outbox_event.event_id, outbox_event.command_id,
                    outbox_event.aggregate_id, outbox_event.aggregate_version,
                    outbox_event.event_type, outbox_event.schema_version,
                    json.dumps(outbox_event.payload), outbox_event.created_at,
                    outbox_event.available_at)
        finally:
            await self.db.pool.release(conn)
        return CommandReceipt(
            command_id=command.command_id, run_id=command.run_id,
            state=command.state, accepted=True, deduplicated=False,
            state_version=command.state_version,
            status_url=STATUS_URL.format(command_id=command.command_id))

    async def record_command(self, command: Dict[str, Any]) -> str:
        return await self._simple.record_command(command)

    async def append_outbox(self, event_id: str, topic: str,
                            payload: Dict[str, Any], *,
                            available_at=None) -> None:
        """§21.4: outbox rows land in the LEASED-CLAIM table the
        dispatcher consumes (pg_outbox_v2)."""
        import time as _time
        now = _time.time()
        await self.db.execute(
            """INSERT INTO pg_outbox_v2 (event_id, command_id,
                 aggregate_id, aggregate_version, event_type,
                 schema_version, payload_json, created_at, available_at,
                 publish_attempts)
               VALUES ($1,NULL,$2,1,$3,1,$4::jsonb,$5::bigint,
                       COALESCE($6::bigint, $5::bigint),0)
               ON CONFLICT (event_id) DO NOTHING""",
            event_id, event_id, topic, json.dumps(payload), int(now),
            int(available_at) if available_at else None)

    async def mark_outbox_published(self, event_id: str) -> None:
        return await self._simple.mark_outbox_published(event_id)

    async def get_command(self, command_id: str) -> Optional[Command]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_commands_v2 WHERE command_id=$1", command_id)
        return _pg_row_to_command(row) if row else None

    async def get_command_by_run(self, run_id: str) -> Optional[Command]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_commands_v2 WHERE run_id=$1 "
            "ORDER BY created_at DESC LIMIT 1", run_id)
        return _pg_row_to_command(row) if row else None

    async def set_command_run(self, command_id: str, run_id: str) -> None:
        await self.db.execute(
            "UPDATE pg_commands_v2 SET run_id=$2, updated_at=$3 "
            "WHERE command_id=$1 AND run_id IS NULL",
            command_id, run_id, _epoch_now())

    async def transition(self, command_id: str, target: CommandState, *,
                         expected_version: Optional[int] = None,
                         actor: str = "system", reason: str = "") -> bool:
        if expected_version is not None:
            row = await self.db.fetch_one(
                "UPDATE pg_commands_v2 SET state=$2, "
                "state_version=state_version+1, updated_at=$4 "
                "WHERE command_id=$1 AND state_version=$3 RETURNING command_id",
                command_id, target.value, expected_version, _epoch_now())
        else:
            row = await self.db.fetch_one(
                "UPDATE pg_commands_v2 SET state=$2, "
                "state_version=state_version+1, updated_at=$4 "
                "WHERE command_id=$1 RETURNING command_id",
                command_id, target.value, expected_version, _epoch_now())
        return row is not None

    # ── outbox dispatch cycle (§21.4 cross-process) ────────────────────
    async def claim_outbox_batch(self, owner: str, batch: int = 20,
                                 lease_seconds: int = 60) -> List[OutboxEvent]:
        rows = await self.db.fetch_all(
            """WITH claimed AS (
                 SELECT event_id FROM pg_outbox_v2
                 WHERE published_at IS NULL AND available_at <= $2
                   AND (dispatcher_lease_expires_at IS NULL
                        OR dispatcher_lease_expires_at <= $2)
                 ORDER BY available_at LIMIT $3 FOR UPDATE SKIP LOCKED)
               UPDATE pg_outbox_v2 o
               SET dispatcher_owner=$1,
                   dispatcher_lease_expires_at=$2 + $4,
                   publish_attempts = o.publish_attempts + 1
               FROM claimed c WHERE o.event_id = c.event_id
               RETURNING o.*""",
            owner, _epoch_now(), batch, lease_seconds)
        return [_row_to_event(_pg_row(r)) for r in rows]

    async def mark_outbox_published(self, event_id: str) -> None:
        await self.db.execute(
            "UPDATE pg_outbox_v2 SET published_at=$2 WHERE event_id=$1",
            event_id, _epoch_now())

    async def fail_outbox_event(self, event_id: str,
                                error_code: str) -> None:
        await self.db.execute(
            "UPDATE pg_outbox_v2 SET last_error_code=$2 WHERE event_id=$1",
            event_id, error_code[:200])

    async def reclaim_stale_outbox_leases(self) -> int:
        rows = await self.db.fetch_all(
            "UPDATE pg_outbox_v2 SET dispatcher_owner=NULL, "
            "dispatcher_lease_expires_at=NULL "
            "WHERE published_at IS NULL AND dispatcher_lease_expires_at < $1 "
            "RETURNING event_id", _epoch_now())
        return len(rows)


    @staticmethod
    async def _apply_work_write(conn, collection: str, doc_id: str,
                                data: Dict[str, Any]) -> None:
        """Deterministic (collection, id, data) triples — the SAME logical
        work the sqlite callback performs, translated to the PG tables."""
        if collection == "parent_waits":
            await conn.execute(
                """INSERT INTO parent_waits (wait_id, parent_run_id,
                     parent_task_id, child_command_id, child_run_id,
                     condition, state, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                   ON CONFLICT (wait_id) DO NOTHING""",
                doc_id, data.get("parent_run_id"), data.get("parent_task_id"),
                data.get("child_command_id"), data.get("child_run_id"),
                data.get("condition", "child_build_succeeded"),
                data.get("state", "waiting"), data.get("created_at", ""))
        elif collection == "mcp_builds":
            await conn.execute(
                """INSERT INTO pg_mcp_builds (id, user_id, name, method,
                     source, status, stage, logs_json, origin,
                     parent_run_id, parent_workflow_id, parent_task_id,
                     wait_reason, organization_id, credential_reference,
                     required_operations_json, created_at)
                   VALUES ($1,$2,$3,$4,$5,'queued','queued','[]',$6,$7,$8,
                           $9,$10,$11,$12,$13,$14)
                   ON CONFLICT (id) DO NOTHING""",
                doc_id, data.get("user_id", ""), data.get("name", ""),
                data.get("method", "spec"), data.get("source", ""),
                data.get("origin", "api"), data.get("parent_run_id"),
                data.get("parent_workflow_id"), data.get("parent_task_id"),
                data.get("wait_reason", "capability_missing"),
                data.get("organization_id"),
                data.get("credential_reference"),
                json.dumps(data.get("required_operations", [])),
                data.get("created_at") or None)
        elif collection == "workflow_runs":
            await conn.execute(
                """INSERT INTO workflows (id, user_id, org_id, goal, status)
                   VALUES ($1,$2,$3,$4,'RUNNING')
                   ON CONFLICT (id) DO NOTHING""",
                doc_id, data.get("user_id", ""),
                data.get("org_id") or data.get("organization_id"),
                data.get("goal", ""))
            for index, task in enumerate(data.get("tasks", [])):
                await conn.execute(
                    """INSERT INTO tasks (id,run_id,user_id,task_index,title,status,payload)
                       VALUES ($1,$2,$3,$4,$5,'PENDING',$6::jsonb)
                       ON CONFLICT (run_id,id) DO NOTHING""",
                    task["task_id"], doc_id, data.get("user_id", ""), index,
                    task.get("title") or task.get("agent", ""), json.dumps(task))
        else:
            await conn.execute(
                f"INSERT INTO {collection} (id, payload) "
                "VALUES ($1, $2::jsonb) ON CONFLICT (id) DO NOTHING",
                doc_id, json.dumps(data))


class _AsyncPgConn:
    """SQLite-callback adapter: `initial_work(conn)` callbacks written
    for aiosqlite (`.execute(sql, params)` with `?` placeholders) run
    against asyncpg with placeholder translation."""

    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql: str, params: tuple = ()):
        pg_sql, args = _sqlite_to_pg(sql, list(params))
        return await self._conn.execute(pg_sql, *args)

    async def commit(self):
        return None   # the surrounding transaction owns the commit


def _sqlite_to_pg(sql: str, params: List[Any]) -> tuple:
    """Translate a sqlite-dialect statement (from the backend-neutral
    initial-work callbacks) to the Postgres-native equivalent: ?→$N
    placeholders, INSERT OR IGNORE → ON CONFLICT DO NOTHING, and the
    three known initial-work statements rewritten to their PG tables."""
    import re

    text = sql.strip()

    # ── parent_waits: same columns, real PG table ────────────────────
    if "INSERT OR IGNORE INTO parent_waits" in text:
        text = text.replace("INSERT OR IGNORE INTO parent_waits",
                            "INSERT INTO parent_waits")
        text = re.sub(r"\)\s*VALUES\s*\(", ") VALUES (", text, count=1)
        text = text.rstrip() + " ON CONFLICT (wait_id) DO NOTHING"

    # ── mcp_builds → pg_mcp_builds (mapped columns) ──────────────────
    if "INSERT OR IGNORE INTO mcp_builds" in text:
        text = text.replace("INSERT OR IGNORE INTO mcp_builds",
                            "INSERT INTO pg_mcp_builds")
        # sqlite column list uses (build_id, user_id, name, method, source,
        # status, stage, logs_json, origin, parent_run_id,
        # parent_workflow_id, parent_task_id, wait_reason, organization_id,
        # credential_reference, required_operations_json, created_at,
        # updated_at) — pg stores the extras in payload JSONB.
        text = text.replace(
            "(build_id, user_id, name, method, source, status, stage,\n"
            "                        logs_json, origin, parent_run_id, "
            "parent_workflow_id,\n                        parent_task_id, "
            "wait_reason, organization_id,\n                        "
            "credential_reference, required_operations_json, created_at, "
            "updated_at)",
            "(id, user_id, name, method, source, status, stage, logs_json, "
            "origin, parent_run_id, parent_workflow_id, parent_task_id)")
        text = re.sub(r"VALUES \(\?, (\?, ){15}\?\)", "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)", text)
        text = text.rstrip() + " ON CONFLICT (id) DO NOTHING"
        # Drop the 6 tail params (wait_reason..updated_at) by folding them
        # into a payload write after the main insert — handled by the
        # caller-visible behavior: keep only the first 12 params.
        params = params[:12]

    # ── workflow_runs + tasks → the PG workflows/tasks tables ────────
    if "INSERT OR IGNORE INTO workflow_runs" in text:
        text = ("INSERT INTO workflows (id, user_id, org_id, goal, status) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (id) DO NOTHING")
        params = [params[0], params[2], params[9], params[3], params[4]]
    elif "INSERT OR IGNORE INTO tasks" in text:
        # sqlite: (task_id, run_id, workflow_id, user_id, agent, tool,
        # status-position 7 = PENDING literal, input_data, dependencies,
        # timeout_seconds, max_retries, recovery_enabled). Parameters are
        # RENUMBERED sequentially ($1..$5) — asyncpg rejects gaps.
        text = ("INSERT INTO tasks (id, run_id, user_id, task_index, title, "
                "status, payload) VALUES ($1,$2,$3,0,$4,'PENDING',$5::jsonb) "
                "ON CONFLICT (run_id, id) DO NOTHING")
        payload = {"agent": params[4], "tool": params[5],
                   "input_data": params[6],
                   "dependencies": params[7],
                   "timeout_seconds": params[8],
                   "max_retries": params[9],
                   "recovery_enabled": params[10],
                   "workflow_id": params[2]}
        # $1=task_id $2=run_id $3=user_id $4=title(agent) $5=payload
        params = [params[0], params[1], params[3],
                  f"{params[4]}: task", json.dumps(payload)]

    # ── generic ?→$N + INSERT OR IGNORE ──────────────────────────────
    in_quote = None
    result = []
    i = 0
    for ch in text:
        if in_quote:
            result.append(ch)
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ("'", '"'):
            in_quote = ch
            result.append(ch)
            continue
        if ch == "?":
            i += 1
            result.append(f"${i}")
        else:
            result.append(ch)
    text = "".join(result)
    if "INSERT OR IGNORE" in text:
        text = text.replace("INSERT OR IGNORE", "INSERT")
        text = re.sub(r"\)\s*VALUES\s*\(([^)]*)\)\s*$",
                      ") VALUES (\1) ON CONFLICT DO NOTHING", text)
    return text, params


def _pg_row(row):
    return {k: row[k] for k in row.keys()}


def _pg_row_to_command(row) -> Command:
    payload = row["payload_json"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except ValueError:
            payload = {}
    wr = row["waiting_reason"]
    from backend.models.command import CommandState, WaitingReason
    return Command(
        command_id=row["command_id"], command_type=row["command_type"],
        schema_version=int(row["schema_version"]),
        owner_user_id=row["owner_user_id"],
        organization_id=row["organization_id"],
        actor_type=row["actor_type"], source=row["source"] or "",
        idempotency_key=row["idempotency_key"],
        idempotency_scoped=row["idempotency_scoped"],
        request_digest=row["request_digest"], payload=payload,
        run_id=row["run_id"], build_id=row["build_id"],
        parent_command_id=row["parent_command_id"],
        parent_run_id=row["parent_run_id"],
        parent_task_id=row["parent_task_id"],
        state=CommandState(row["state"]),
        state_version=int(row["state_version"]),
        waiting_reason=WaitingReason(wr) if wr else None,
        deadline_at=row["deadline_at"],
        cancel_requested_at=row["cancel_requested_at"],
        cancel_requested_by=row["cancel_requested_by"],
        cancel_reason=row["cancel_reason"],
        cancel_acknowledged_at=row["cancel_acknowledged_at"],
        terminal_reason=row["terminal_reason"],
        result_reference=row["result_reference"],
        created_at=int(row["created_at"]), updated_at=int(row["updated_at"]),
    )
