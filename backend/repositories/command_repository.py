"""Command + outbox repositories (Phase 4 §4.5–§4.7).

The atomic acceptance operation: command + scoped idempotency + queue
admission + initial work + outbox event commit in ONE transaction (SQLite:
BEGIN IMMEDIATE on a dedicated connection via DatabaseManager.write_transaction;
Firestore: one transaction with pre-reads). The network publish happens
AFTERWARD via the outbox dispatcher — a crash between commit and publish is
recovered by re-publishing, so consumers must tolerate duplicates (§4.8).

§4.6 requirements honored: no independent-committing repository calls inside
the acceptance operation, rollback on every exception, scoped unique
identities, parameterized queries, no external calls while holding the
transaction, SQLITE_BUSY bounded by write_transaction.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol

from backend.models.admission import epoch_now
from backend.models.command import (
    Command,
    CommandReceipt,
    CommandState,
    CommandTransitionError,
    OutboxEvent,
    WaitingReason,
    transition_is_valid,
)

logger = logging.getLogger(__name__)

STATUS_URL = "/api/v1/commands/{command_id}"


class CommandRepository(Protocol):
    async def accept_command_with_initial_work(
        self, command: Command, admission, outbox_event: OutboxEvent,
        initial_work: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> CommandReceipt: ...

    async def get_command(self, command_id: str) -> Optional[Command]: ...

    async def transition(self, command_id: str, target: CommandState,
                         *, expected_state_version: Optional[int] = None,
                         actor: str = "system", reason: str = "",
                         waiting_reason: Optional[WaitingReason] = None,
                         terminal_reason: Optional[str] = None,
                         result_reference: Optional[str] = None,
                         outbox_event: Optional[OutboxEvent] = None) -> Command: ...

    async def record_cancellation_intent(self, command_id: str, by: str,
                                         reason: str = "") -> Command: ...

    async def append_outbox(self, event: OutboxEvent) -> None: ...

    async def claim_outbox_batch(self, dispatcher_owner: str, batch: int,
                                 lease_seconds: int) -> list: ...

    async def mark_outbox_published(self, event_id: str) -> bool: ...

    async def fail_outbox_event(self, event_id: str, error_code: str) -> None: ...

    async def reclaim_stale_outbox_leases(self) -> int: ...


def _row_to_command(row) -> Command:
    payload = row["payload_json"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except ValueError:
            payload = {}
    wr = row["waiting_reason"]
    return Command(
        command_id=row["command_id"], command_type=row["command_type"],
        schema_version=int(row["schema_version"]),
        owner_user_id=row["owner_user_id"], organization_id=row["organization_id"],
        actor_type=row["actor_type"], source=row["source"] or "",
        idempotency_key=row["idempotency_key"],
        idempotency_scoped=row["idempotency_scoped"],
        request_digest=row["request_digest"],
        payload=payload if isinstance(payload, dict) else {},
        run_id=row["run_id"], build_id=row["build_id"],
        parent_command_id=row["parent_command_id"],
        parent_run_id=row["parent_run_id"], parent_task_id=row["parent_task_id"],
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


def _row_to_event(row) -> OutboxEvent:
    payload = row["payload_json"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except ValueError:
            payload = {}
    return OutboxEvent(
        event_id=row["event_id"], command_id=row["command_id"],
        aggregate_id=row["aggregate_id"],
        aggregate_version=int(row["aggregate_version"]),
        event_type=row["event_type"], schema_version=int(row["schema_version"]),
        payload=payload if isinstance(payload, dict) else {},
        created_at=int(row["created_at"]), available_at=int(row["available_at"]),
        publish_attempts=int(row["publish_attempts"]),
        dispatcher_owner=row["dispatcher_owner"],
        dispatcher_lease_expires_at=row["dispatcher_lease_expires_at"],
        published_at=row["published_at"],
        last_error_code=row["last_error_code"],
    )


class SQLiteCommandRepository:
    """Transactional acceptance + fenced transitions on SQLite."""

    def __init__(self, db):
        self.db = db

    async def accept_command_with_initial_work(
        self, command: Command, admission, outbox_event: OutboxEvent,
        initial_work: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> CommandReceipt:
        """§4.5: commit command + idempotency + queue accounting + initial
        work + outbox in ONE transaction; return the receipt only once
        acceptance is durable. `admission` is the queue-admission outcome
        already computed inside THIS transaction by the caller service
        (admission checks run on the same owned connection)."""
        now = epoch_now()
        command.created_at = now
        command.updated_at = now
        if outbox_event.created_at == 0:
            outbox_event.created_at = now
        if outbox_event.available_at == 0:
            outbox_event.available_at = now
        async with self.db.write_transaction() as conn:
            # ── Scoped idempotency (§4.4 contract) ──────────────────────
            if command.idempotency_scoped:
                cursor = await conn.execute(
                    "SELECT command_id, request_digest, state, state_version, run_id "
                    "FROM commands WHERE idempotency_scoped=?",
                    (command.idempotency_scoped,))
                row = await cursor.fetchone()
                if row:
                    if (command.request_digest is not None
                            and row["request_digest"]
                            and row["request_digest"] != command.request_digest):
                        return CommandReceipt(
                            command_id=row["command_id"], run_id=row["run_id"],
                            state=CommandState(row["state"]),
                            accepted=False, deduplicated=False,
                            state_version=int(row["state_version"]),
                            status_url=STATUS_URL.format(command_id=row["command_id"]),
                            error_code=_conflict_code(),
                            message="Same idempotency key with a different request payload")
                    return CommandReceipt(
                        command_id=row["command_id"], run_id=row["run_id"],
                        state=CommandState(row["state"]),
                        accepted=True, deduplicated=True,
                        state_version=int(row["state_version"]),
                        status_url=STATUS_URL.format(command_id=row["command_id"]))

            # ── Queue admission accounting (same transaction, R7/R9) ────
            admission_row = None
            if admission is not None:
                pending = await conn.execute(
                    "SELECT COUNT(*) FROM pending_commands "
                    "WHERE status IN ('pending','dispatched') AND user_id=?",
                    (command.owner_user_id,))
                in_queue = int((await pending.fetchone())[0])
                limit = int(admission.max_pending_per_user or 0)
                if limit > 0 and in_queue >= limit:
                    return CommandReceipt(
                        command_id=command.command_id, run_id=None,
                        state=command.state, accepted=False, deduplicated=False,
                        state_version=0, status_url="",
                        error_code=_queue_full_code(),
                        message="The workspace has too much work waiting")
                admission_row = admission

            # ── Command row ──────────────────────────────────────────────
            row = command.to_row()
            await conn.execute(
                """
                INSERT INTO commands
                    (command_id, command_type, schema_version, owner_user_id,
                     organization_id, actor_type, source, idempotency_key,
                     idempotency_scoped, request_digest, payload_json, run_id,
                     build_id, parent_command_id, parent_run_id, parent_task_id,
                     state, state_version, waiting_reason, deadline_at,
                     cancel_requested_at, cancel_requested_by, cancel_reason,
                     cancel_acknowledged_at, terminal_reason, result_reference,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row["command_id"], row["command_type"], row["schema_version"],
                 row["owner_user_id"], row["organization_id"], row["actor_type"],
                 row["source"], row["idempotency_key"], row["idempotency_scoped"],
                 row["request_digest"], json.dumps(row["payload_json"]), row["run_id"],
                 row["build_id"], row["parent_command_id"], row["parent_run_id"],
                 row["parent_task_id"], row["state"], row["state_version"],
                 row["waiting_reason"], row["deadline_at"], row["cancel_requested_at"],
                 row["cancel_requested_by"], row["cancel_reason"],
                 row["cancel_acknowledged_at"], row["terminal_reason"],
                 row["result_reference"], row["created_at"], row["updated_at"]),
            )

            # ── Queue-admission record (§4.17 accepted-not-started counts) ──
            if admission_row is not None:
                await conn.execute(
                    "INSERT INTO pending_commands (command_id, user_id, org_id, "
                    "workload_class, status, idempotency_key, request_digest, "
                    "created_at, expires_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                    (command.command_id, command.owner_user_id,
                     command.organization_id, command.command_type,
                     command.idempotency_scoped, command.request_digest,
                     now, now + admission_row.max_wait_seconds, now))

            # ── Initial work (caller-supplied, SAME transaction) ────────
            if initial_work is not None:
                await initial_work(conn)

            # ── Outbox event ─────────────────────────────────────────────
            await conn.execute(
                """
                INSERT INTO outbox_events
                    (event_id, command_id, aggregate_id, aggregate_version,
                     event_type, schema_version, payload_json, created_at,
                     available_at, publish_attempts, dispatcher_owner,
                     dispatcher_lease_expires_at, published_at, last_error_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL)
                """,
                (outbox_event.event_id, outbox_event.command_id,
                 outbox_event.aggregate_id, outbox_event.aggregate_version,
                 outbox_event.event_type, outbox_event.schema_version,
                 json.dumps(outbox_event.payload), outbox_event.created_at,
                 outbox_event.available_at),
            )
        return CommandReceipt(
            command_id=command.command_id, run_id=command.run_id,
            state=command.state, accepted=True, deduplicated=False,
            state_version=command.state_version,
            status_url=STATUS_URL.format(command_id=command.command_id))

    async def get_command(self, command_id: str) -> Optional[Command]:
        cursor = await self.db.execute(
            "SELECT * FROM commands WHERE command_id=?", (command_id,))
        row = await cursor.fetchone()
        return _row_to_command(row) if row else None

    async def get_command_by_run(self, run_id: str) -> Optional[Command]:
        """C4 bridge: the command that created a run (any state)."""
        cursor = await self.db.execute(
            "SELECT * FROM commands WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
            (run_id,))
        row = await cursor.fetchone()
        return _row_to_command(row) if row else None

    async def set_command_run(self, command_id: str, run_id: str) -> None:
        """C5: link a pre-accepted command to the run created downstream."""
        await self.db.execute(
            "UPDATE commands SET run_id=?, updated_at=? WHERE command_id=? "
            "AND run_id IS NULL", (run_id, epoch_now(), command_id))
        await self.db.commit()

    async def find_deadline_expired(self, now: int, limit: int = 50) -> list:
        """§4.18 (C12): backend-neutral reconciler query — accepted/queued
        commands past their deadline."""
        cursor = await self.db.execute(
            "SELECT command_id, state, state_version FROM commands WHERE "
            "state IN ('ACCEPTED','QUEUED','RUNNING','WAITING',"
            "'CANCEL_REQUESTED') AND deadline_at IS NOT NULL AND "
            "deadline_at<=? LIMIT ?", (now, limit))
        return [dict(r) for r in await cursor.fetchall()]

    async def find_unacknowledged_cancellations(self, older_than: int,
                                                limit: int = 50) -> list:
        """§4.18 (C12): cancellations awaiting acknowledgement past the
        SLA — the reconciler escalates them."""
        cursor = await self.db.execute(
            "SELECT command_id, cancel_requested_at FROM commands WHERE "
            "state='CANCEL_REQUESTED' AND cancel_acknowledged_at IS NULL "
            "AND cancel_requested_at<=? LIMIT ?", (older_than, limit))
        return [dict(r) for r in await cursor.fetchall()]

    async def expire_command(self, command_id: str, *, actor: str = "reconciler",
                             reason: str = "deadline exceeded") -> bool:
        """§4.18: bounded repair — a deadline-expired command transitions
        to EXPIRED (CAS from its current state)."""
        from backend.models.command import CommandState, CommandTransitionError
        try:
            command = await self.get_command(command_id)
            if command is None or command.terminal:
                return False
            await self.transition(command_id, CommandState.EXPIRED,
                                  expected_state_version=command.state_version,
                                  actor=actor, reason=reason)
            return True
        except CommandTransitionError:
            return False

    async def command_events(self, command_id: str, limit: int = 100) -> list:
        """C12: backend-neutral event history (no raw SQL at call sites)."""
        cursor = await self.db.execute(
            "SELECT event_id, event_type, aggregate_version, schema_version, "
            "payload_json, created_at, published_at FROM outbox_events "
            "WHERE command_id=? ORDER BY created_at DESC LIMIT ?",
            (command_id, limit))
        rows = []
        for r in await cursor.fetchall():
            row = dict(r)
            try:
                row["payload"] = json.loads(row.pop("payload_json") or "{}")
            except ValueError:
                row["payload"] = {}
            rows.append(row)
        return rows

    async def get_command_by_scoped_key(self, scoped: str) -> Optional[Command]:
        cursor = await self.db.execute(
            "SELECT * FROM commands WHERE idempotency_scoped=?", (scoped,))
        row = await cursor.fetchone()
        return _row_to_command(row) if row else None

    async def transition(self, command_id: str, target: CommandState,
                         *, expected_state_version: Optional[int] = None,
                         actor: str = "system", reason: str = "",
                         waiting_reason: Optional[WaitingReason] = None,
                         terminal_reason: Optional[str] = None,
                         result_reference: Optional[str] = None,
                         outbox_event: Optional[OutboxEvent] = None) -> Command:
        """§4.3: a conditional transition — validates the allowed source
        states, the expected state version, and cancellation interference;
        bumps state_version monotonically; appends the follow-up outbox event
        IN THE SAME transaction when supplied."""
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM commands WHERE command_id=?", (command_id,))
            row = await cursor.fetchone()
            if not row:
                raise CommandTransitionError(
                    f"Command {command_id} not found", CommandState.FAILED, 0,
                    _not_found_code())
            current = _row_to_command(row)
            if current.terminal:
                raise CommandTransitionError(
                    f"Command {command_id} is terminal ({current.state.value})",
                    current.state, current.state_version)
            if not transition_is_valid(current.state, target):
                raise CommandTransitionError(
                    f"Invalid transition {current.state.value} → {target.value} "
                    f"for {command_id}", current.state, current.state_version)
            if expected_state_version is not None and \
                    int(row["state_version"]) != int(expected_state_version):
                raise CommandTransitionError(
                    f"State version conflict for {command_id}: durable "
                    f"{row['state_version']}, caller expected {expected_state_version}",
                    current.state, int(row["state_version"]))
            # A cancellation that was REQUESTED blocks SUCCEEDED unless the
            # work honestly completed before cancellation took effect — that
            # race is resolved by policy: completion commits first, wins.
            new_version = int(row["state_version"]) + 1
            cursor = await conn.execute(
                """
                UPDATE commands SET state=?, state_version=?, waiting_reason=?,
                    terminal_reason=?, result_reference=?, updated_at=?
                WHERE command_id=? AND state_version=?
                """,
                (target.value, new_version,
                 waiting_reason.value if waiting_reason else None,
                 terminal_reason, result_reference, now,
                 command_id, int(row["state_version"])),
            )
            if not cursor.rowcount:
                raise CommandTransitionError(
                    f"Concurrent state change for {command_id}",
                    current.state, int(row["state_version"]))
            if outbox_event is not None:
                if outbox_event.created_at == 0:
                    outbox_event.created_at = now
                if outbox_event.available_at == 0:
                    outbox_event.available_at = now
                await conn.execute(
                    """
                    INSERT INTO outbox_events
                        (event_id, command_id, aggregate_id, aggregate_version,
                         event_type, schema_version, payload_json, created_at,
                         available_at, publish_attempts, dispatcher_owner,
                         dispatcher_lease_expires_at, published_at, last_error_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL)
                    """,
                    (outbox_event.event_id, outbox_event.command_id,
                     outbox_event.aggregate_id, new_version,
                     outbox_event.event_type, outbox_event.schema_version,
                     json.dumps(outbox_event.payload), outbox_event.created_at,
                     outbox_event.available_at),
                )
            # C3 (§5.0 Batch A): queue accounting follows the documented
            # definition — accepted-NOT-STARTED. Work that STARTED (RUNNING)
            # or reached a terminal state frees its slot: the command row
            # owns the lifecycle from here, not the pending queue.
            if target is CommandState.RUNNING:
                await conn.execute(
                    "UPDATE pending_commands SET status='dispatched', updated_at=? "
                    "WHERE command_id=? AND status='pending'", (now, command_id))
            elif target in (CommandState.SUCCEEDED, CommandState.FAILED,
                            CommandState.CANCELLED, CommandState.EXPIRED):
                await conn.execute(
                    "UPDATE pending_commands SET status='settled', updated_at=? "
                    "WHERE command_id=? AND status IN ('pending','dispatched')",
                    (now, command_id))
        # Read-back AFTER the transaction commits (the shared connection
        # cannot see uncommitted owned-transaction data).
        return await self.get_command(command_id)

    async def record_cancellation_intent(self, command_id: str, by: str,
                                         reason: str = "") -> Command:
        """§4.15 step 1: persist cancellation intent (idempotent). Terminal
        commands are returned unchanged — cancellation after completion does
        not resurrect or rewrite the outcome."""
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM commands WHERE command_id=?", (command_id,))
            row = await cursor.fetchone()
            if not row:
                raise CommandTransitionError(
                    f"Command {command_id} not found", CommandState.FAILED, 0,
                    _not_found_code())
            current = _row_to_command(row)
            if current.terminal:
                return current   # completion committed before cancellation: honest
            if current.state is CommandState.CANCEL_REQUESTED:
                return current   # duplicate cancellation: same result
            target = (CommandState.CANCEL_REQUESTED
                      if current.state is not CommandState.CANCEL_REQUESTED
                      else current.state)
            new_version = int(row["state_version"]) + 1
            await conn.execute(
                """
                UPDATE commands SET state=?, state_version=?, cancel_requested_at=?,
                    cancel_requested_by=?, cancel_reason=?, updated_at=?
                WHERE command_id=? AND state_version=?
                """,
                (target.value, new_version, now, by, reason, now,
                 command_id, int(row["state_version"])),
            )
        return await self.get_command(command_id)

    async def acknowledge_cancellation(self, command_id: str) -> Command:
        now = epoch_now()
        await self.db.execute(
            "UPDATE commands SET cancel_acknowledged_at=?, updated_at=? "
            "WHERE command_id=? AND cancel_requested_at IS NOT NULL "
            "AND cancel_acknowledged_at IS NULL",
            (now, now, command_id))
        await self.db.commit()
        return await self.get_command(command_id)

    # ── Outbox (§4.8) ─────────────────────────────────────────────────

    async def append_outbox(self, event: OutboxEvent) -> None:
        now = epoch_now()
        if event.created_at == 0:
            event.created_at = now
        if event.available_at == 0:
            event.available_at = now
        await self.db.execute(
            """
            INSERT OR IGNORE INTO outbox_events
                (event_id, command_id, aggregate_id, aggregate_version,
                 event_type, schema_version, payload_json, created_at,
                 available_at, publish_attempts, dispatcher_owner,
                 dispatcher_lease_expires_at, published_at, last_error_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL)
            """,
            (event.event_id, event.command_id, event.aggregate_id,
             event.aggregate_version, event.event_type, event.schema_version,
             json.dumps(event.payload), event.created_at, event.available_at))
        await self.db.commit()

    async def claim_outbox_batch(self, dispatcher_owner: str, batch: int,
                                 lease_seconds: int) -> list:
        """Lease-claimed batch of eligible events: another dispatcher can
        recover abandoned claims after the lease expires (§4.8 step 1-2)."""
        now = epoch_now()
        lease_until = now + lease_seconds
        await self.db.commit()
        await self.db.execute(
            "UPDATE outbox_events SET dispatcher_owner=NULL, "
            "dispatcher_lease_expires_at=NULL WHERE dispatcher_owner IS NOT NULL "
            "AND dispatcher_lease_expires_at<=?", (now,))
        await self.db.commit()
        cursor = await self.db.execute(
            "SELECT * FROM outbox_events WHERE published_at IS NULL "
            "AND available_at<=? AND dispatcher_owner IS NULL "
            "ORDER BY created_at LIMIT ?", (now, batch))
        rows = await cursor.fetchall()
        claimed = []
        for row in rows:
            c = await self.db.execute(
                "UPDATE outbox_events SET dispatcher_owner=?, "
                "dispatcher_lease_expires_at=? WHERE event_id=? "
                "AND dispatcher_owner IS NULL",
                (dispatcher_owner, lease_until, row["event_id"]))
            if c.rowcount:
                claimed.append(_row_to_event(row))
        await self.db.commit()
        return claimed

    async def mark_outbox_published(self, event_id: str) -> bool:
        """§4.8 step 4: NEVER called before the transport confirms."""
        cursor = await self.db.execute(
            "UPDATE outbox_events SET published_at=?, dispatcher_owner=NULL, "
            "dispatcher_lease_expires_at=NULL WHERE event_id=? "
            "AND published_at IS NULL",
            (epoch_now(), event_id))
        await self.db.commit()
        return bool(cursor.rowcount)

    async def fail_outbox_event(self, event_id: str, error_code: str) -> None:
        """§4.8 step 5: bounded backoff via available_at doubling."""
        cursor = await self.db.execute(
            "SELECT publish_attempts FROM outbox_events WHERE event_id=?",
            (event_id,))
        row = await cursor.fetchone()
        if not row:
            return
        attempts = int(row["publish_attempts"]) + 1
        delay = min(300, 2 ** min(attempts, 8))
        await self.db.execute(
            "UPDATE outbox_events SET publish_attempts=?, available_at=?, "
            "last_error_code=?, dispatcher_owner=NULL, "
            "dispatcher_lease_expires_at=NULL WHERE event_id=?",
            (attempts, epoch_now() + delay, error_code, event_id))
        await self.db.commit()

    async def reclaim_stale_outbox_leases(self) -> int:
        cursor = await self.db.execute(
            "UPDATE outbox_events SET dispatcher_owner=NULL, "
            "dispatcher_lease_expires_at=NULL WHERE published_at IS NULL "
            "AND dispatcher_owner IS NOT NULL AND dispatcher_lease_expires_at<=?",
            (epoch_now(),))
        await self.db.commit()
        return cursor.rowcount or 0

    async def outbox_stats(self) -> dict:
        """§4.22: oldest unpublished age + backlog (bounded labels)."""
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM outbox_events "
            "WHERE published_at IS NULL")
        row = await cursor.fetchone()
        return {
            "unpublished": int(row["n"]),
            "oldest_unpublished_age_seconds": max(0, now - int(row["oldest"]))
            if row["oldest"] else 0,
        }


def _conflict_code():
    from backend.models.command import CommandErrorCode
    return CommandErrorCode.IDEMPOTENCY_CONFLICT


def _queue_full_code():
    from backend.models.command import CommandErrorCode
    return CommandErrorCode.QUEUE_FULL


def _not_found_code():
    from backend.models.command import CommandErrorCode
    return CommandErrorCode.NOT_FOUND
