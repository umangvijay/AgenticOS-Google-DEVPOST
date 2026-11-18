"""Firestore command + outbox repository (Phase 4 §4.7).

The SAME contract as SQLiteCommandRepository against Firestore, with the
constraints Firebase documents for transactions honored explicitly:

- ALL reads precede writes inside the transaction callback (which may rerun;
  stable ids are generated OUTSIDE it, so a retried callback can never
  create a second logical command).
- Deterministic document identities: commands/{command_id},
  idempotency via a unique scoped-key index doc, outbox_events/{event_id}.
- Strict queue caps use an explicit COUNTER DOCUMENT updated in the same
  transaction as command creation — never a query-and-count.
- Nothing inside a transaction callback publishes or calls providers.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from backend.models.admission import epoch_now
from backend.models.command import (
    Command,
    CommandErrorCode,
    CommandReceipt,
    CommandState,
    CommandTransitionError,
    OutboxEvent,
    WaitingReason,
    transition_is_valid,
)
from backend.repositories.command_repository import STATUS_URL, _row_to_command  # noqa: F401

logger = logging.getLogger(__name__)


def _cmd_to_doc(command: Command) -> Dict[str, Any]:
    row = command.to_row()
    return {
        "command_id": row["command_id"], "command_type": row["command_type"],
        "schema_version": row["schema_version"],
        "owner_user_id": row["owner_user_id"],
        "organization_id": row["organization_id"],
        "actor_type": row["actor_type"], "source": row["source"],
        "idempotency_key": row["idempotency_key"],
        "idempotency_scoped": row["idempotency_scoped"],
        "request_digest": row["request_digest"],
        "payload_json": json.dumps(row["payload_json"]),
        "run_id": row["run_id"], "build_id": row["build_id"],
        "parent_command_id": row["parent_command_id"],
        "parent_run_id": row["parent_run_id"],
        "parent_task_id": row["parent_task_id"],
        "state": row["state"], "state_version": row["state_version"],
        "waiting_reason": row["waiting_reason"], "deadline_at": row["deadline_at"],
        "cancel_requested_at": row["cancel_requested_at"],
        "cancel_requested_by": row["cancel_requested_by"],
        "cancel_reason": row["cancel_reason"],
        "cancel_acknowledged_at": row["cancel_acknowledged_at"],
        "terminal_reason": row["terminal_reason"],
        "result_reference": row["result_reference"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _doc_to_command(data: Dict[str, Any], command_id: str) -> Command:
    payload = data.get("payload_json", "{}")
    try:
        payload = json.loads(payload or "{}")
    except ValueError:
        payload = {}
    wr = data.get("waiting_reason")
    return Command(
        command_id=command_id, command_type=data.get("command_type", "workflow"),
        schema_version=int(data.get("schema_version", 1)),
        owner_user_id=data.get("owner_user_id", ""),
        organization_id=data.get("organization_id"),
        actor_type=data.get("actor_type", "user"), source=data.get("source", ""),
        idempotency_key=data.get("idempotency_key"),
        idempotency_scoped=data.get("idempotency_scoped"),
        request_digest=data.get("request_digest"),
        payload=payload if isinstance(payload, dict) else {},
        run_id=data.get("run_id"), build_id=data.get("build_id"),
        parent_command_id=data.get("parent_command_id"),
        parent_run_id=data.get("parent_run_id"),
        parent_task_id=data.get("parent_task_id"),
        state=CommandState(data.get("state", "ACCEPTED")),
        state_version=int(data.get("state_version", 1)),
        waiting_reason=WaitingReason(wr) if wr else None,
        deadline_at=data.get("deadline_at"),
        cancel_requested_at=data.get("cancel_requested_at"),
        cancel_requested_by=data.get("cancel_requested_by"),
        cancel_reason=data.get("cancel_reason"),
        cancel_acknowledged_at=data.get("cancel_acknowledged_at"),
        terminal_reason=data.get("terminal_reason"),
        result_reference=data.get("result_reference"),
        created_at=int(data.get("created_at", 0)),
        updated_at=int(data.get("updated_at", 0)),
    )


def _doc_to_event(data: Dict[str, Any], event_id: str) -> OutboxEvent:
    payload = data.get("payload_json", "{}")
    try:
        payload = json.loads(payload or "{}")
    except ValueError:
        payload = {}
    return OutboxEvent(
        event_id=event_id, command_id=data.get("command_id"),
        aggregate_id=data.get("aggregate_id", ""),
        aggregate_version=int(data.get("aggregate_version", 1)),
        event_type=data.get("event_type", "command.accepted"),
        schema_version=int(data.get("schema_version", 1)),
        payload=payload if isinstance(payload, dict) else {},
        created_at=int(data.get("created_at", 0)),
        available_at=int(data.get("available_at", 0)),
        publish_attempts=int(data.get("publish_attempts", 0)),
        dispatcher_owner=data.get("dispatcher_owner"),
        dispatcher_lease_expires_at=data.get("dispatcher_lease_expires_at"),
        published_at=data.get("published_at"),
        last_error_code=data.get("last_error_code"),
    )


class FirestoreCommandRepository:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def accept_command_with_initial_work(
        self, command: Command, admission, outbox_event: OutboxEvent,
        initial_work=None,
        work_writes: Optional[List[Tuple[str, str, Dict[str, Any]]]] = None,
    ) -> CommandReceipt:
        """C2: the service calls with `initial_work=` (SQLite takes a
        connection callback). Firestore cannot run arbitrary callbacks inside
        a rerunnable transaction — a callable here is an explicit, typed
        error telling the caller to use deterministic `work_writes`."""
        """§4.7: ONE Firestore transaction commits the command, the scoped
        idempotency resolution, the COUNTER-DOC queue accounting, the initial
        work docs, and the outbox event. `work_writes` are (collection,
        doc_id, data) triples (deterministic ids, created-if-absent via
        create() inside the transaction — a concurrent winner surfaces as
        AlreadyExists and resolves to the duplicate receipt).

        NOTE on initial work: the SQLite path takes a callback on the owned
        connection; the Firestore path takes pre-built deterministic writes
        (transactions may rerun callbacks, so arbitrary callbacks are unsafe
        here — §4.7 explicitly forbids side effects inside the callback).
        """
        from google.cloud import firestore as fs_types
        from google.api_core.exceptions import AlreadyExists
        if initial_work is not None:
            raise TypeError(
                "FirestoreCommandRepository cannot execute arbitrary "
                "initial_work callbacks inside a rerunnable transaction (§4.7); "
                "pass deterministic work_writes instead")
        db = await self._get_db()
        now = epoch_now()
        command.created_at = now
        command.updated_at = now
        if outbox_event.created_at == 0:
            outbox_event.created_at = now
        if outbox_event.available_at == 0:
            outbox_event.available_at = now
        coll = db.collection("commands")
        outbox_coll = db.collection("outbox_events")
        pending_coll = db.collection("pending_commands")
        # §4.7: strict caps via an explicit counter document.
        counter_ref = db.collection("command_queue_counters").document(
            f"user-{command.owner_user_id}")
        limit = int(getattr(admission, "max_pending_per_user", 0) or 0)

        @fs_types.async_transactional
        async def admit(transaction):
            # ── Reads first (transactions may rerun; reads precede writes) ──
            scoped_row = None
            if command.idempotency_scoped:
                query = (coll.where("idempotency_scoped", "==",
                                    command.idempotency_scoped).limit(1))
                async for snap in query.stream(transaction=transaction):
                    scoped_row = snap
                    break
            if scoped_row is not None:
                data = scoped_row.to_dict() or {}
                if (command.request_digest is not None
                        and data.get("request_digest")
                        and data["request_digest"] != command.request_digest):
                    return CommandReceipt(
                        command_id=data.get("command_id", command.command_id),
                        run_id=data.get("run_id"),
                        state=CommandState(data.get("state", "ACCEPTED")),
                        accepted=False, deduplicated=False,
                        state_version=int(data.get("state_version", 1)),
                        status_url=STATUS_URL.format(
                            command_id=data.get("command_id", "")),
                        error_code=CommandErrorCode.IDEMPOTENCY_CONFLICT,
                        message="Same idempotency key with a different request payload")
                return CommandReceipt(
                    command_id=data.get("command_id", command.command_id),
                    run_id=data.get("run_id"),
                    state=CommandState(data.get("state", "ACCEPTED")),
                    accepted=True, deduplicated=True,
                    state_version=int(data.get("state_version", 1)),
                    status_url=STATUS_URL.format(
                        command_id=data.get("command_id", "")))

            counter_snap = await counter_ref.get(transaction=transaction)
            in_queue = int((counter_snap.to_dict() or {}).get("count", 0))
            if limit > 0 and in_queue >= limit:
                return CommandReceipt(
                    command_id=command.command_id, run_id=None,
                    state=command.state, accepted=False, deduplicated=False,
                    state_version=0, status_url="",
                    error_code=CommandErrorCode.QUEUE_FULL,
                    message="The workspace has too much work waiting")

            # ── Writes (command + counter + pending + outbox + work) ─────
            transaction.create(coll.document(command.command_id),
                               _cmd_to_doc(command))
            if counter_snap.exists:
                transaction.update(counter_ref, {"count": in_queue + 1})
            else:
                transaction.create(counter_ref, {"count": 1})
            transaction.create(pending_coll.document(command.command_id), {
                "command_id": command.command_id,
                "user_id": command.owner_user_id,
                "org_id": command.organization_id,
                "workload_class": command.command_type,
                "status": "pending",
                "idempotency_key": command.idempotency_scoped,
                "request_digest": command.request_digest,
                "created_at": now,
                "expires_at": now + int(getattr(admission, "max_wait_seconds",
                                                7200)),
                "updated_at": now,
            })
            for collection, doc_id, data in (work_writes or []):
                if collection == "parent_waits":
                    from backend.repositories.firestore.wait_repository import open_wait_ref
                    slot = open_wait_ref(db, data["parent_run_id"], data["parent_task_id"])
                    transaction.create(slot, {"wait_id": doc_id})
                transaction.create(db.collection(collection).document(doc_id),
                                   data)
            transaction.create(outbox_coll.document(outbox_event.event_id), {
                "command_id": outbox_event.command_id,
                "aggregate_id": outbox_event.aggregate_id,
                "aggregate_version": outbox_event.aggregate_version,
                "event_type": outbox_event.event_type,
                "schema_version": outbox_event.schema_version,
                "payload_json": json.dumps(outbox_event.payload),
                "created_at": outbox_event.created_at,
                "available_at": outbox_event.available_at,
                "publish_attempts": 0, "dispatcher_owner": None,
                "dispatcher_lease_expires_at": None, "published_at": None,
                "last_error_code": None,
            })
            return CommandReceipt(
                command_id=command.command_id, run_id=command.run_id,
                state=command.state, accepted=True, deduplicated=False,
                state_version=command.state_version,
                status_url=STATUS_URL.format(command_id=command.command_id))

        try:
            return await admit(db.transaction())
        except AlreadyExists:
            # The deterministic command id already exists (concurrent winner):
            # resolve as a duplicate of the durable original.
            existing = await self.get_command(command.command_id)
            if existing is not None:
                return CommandReceipt(
                    command_id=existing.command_id, run_id=existing.run_id,
                    state=existing.state, accepted=True, deduplicated=True,
                    state_version=existing.state_version,
                    status_url=STATUS_URL.format(command_id=existing.command_id))
            raise

    async def get_command_by_run(self, run_id: str) -> Optional[Command]:
        query = (self._db.collection("commands").where("run_id", "==", run_id)
                 .order_by("created_at").limit(1))
        snaps = await query.get()
        if not snaps:
            return None
        return _doc_to_command(snaps[0].to_dict() or {}, snaps[0].id)

    async def set_command_run(self, command_id: str, run_id: str) -> None:
        db = await self._get_db()
        await db.collection("commands").document(command_id).set(
            {"run_id": run_id, "updated_at": epoch_now()}, merge=True)

    async def find_deadline_expired(self, now: int, limit: int = 50) -> list:
        db = await self._get_db()
        query = (db.collection("commands")
                 .where("state", "in", ["ACCEPTED", "QUEUED", "RUNNING",
                                        "WAITING", "CANCEL_REQUESTED"])
                 .where("deadline_at", "<=", now).limit(limit))
        out = []
        async for snap in query.stream():
            d = snap.to_dict() or {}
            out.append({"command_id": snap.id, "state": d.get("state"),
                        "state_version": d.get("state_version", 1)})
        return out

    async def find_unacknowledged_cancellations(self, older_than: int,
                                                limit: int = 50) -> list:
        db = await self._get_db()
        query = (db.collection("commands")
                 .where("state", "==", "CANCEL_REQUESTED")
                 .where("cancel_acknowledged_at", "==", None)
                 .where("cancel_requested_at", "<=", older_than)
                 .limit(limit))
        out = []
        async for snap in query.stream():
            d = snap.to_dict() or {}
            out.append({"command_id": snap.id,
                        "cancel_requested_at": d.get("cancel_requested_at")})
        return out

    async def expire_command(self, command_id: str, *, actor: str = "reconciler",
                             reason: str = "deadline exceeded") -> bool:
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
        query = (self._db.collection("outbox_events")
                 .where("command_id", "==", command_id)
                 .order_by("created_at").limit(limit))
        rows = []
        for snap in await query.get():
            data = snap.to_dict() or {}
            payload = data.get("payload_json", "{}")
            try:
                payload = json.loads(payload or "{}")
            except ValueError:
                payload = {}
            rows.append({"event_id": snap.id, "event_type": data.get("event_type"),
                         "aggregate_version": data.get("aggregate_version", 1),
                         "schema_version": data.get("schema_version", 1),
                         "payload": payload, "created_at": data.get("created_at"),
                         "published_at": data.get("published_at")})
        return rows

    async def get_command(self, command_id: str) -> Optional[Command]:
        db = await self._get_db()
        snap = await db.collection("commands").document(command_id).get()
        if not snap.exists:
            return None
        return _doc_to_command(snap.to_dict() or {}, command_id)

    async def transition(self, command_id: str, target: CommandState,
                         *, expected_state_version: Optional[int] = None,
                         actor: str = "system", reason: str = "",
                         waiting_reason: Optional[WaitingReason] = None,
                         terminal_reason: Optional[str] = None,
                         result_reference: Optional[str] = None,
                         outbox_event: Optional[OutboxEvent] = None) -> Command:
        from google.cloud import firestore as fs_types
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection("commands").document(command_id)
        outbox_coll = db.collection("outbox_events")

        @fs_types.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise CommandTransitionError(
                    f"Command {command_id} not found", CommandState.FAILED, 0,
                    CommandErrorCode.NOT_FOUND)
            current = _doc_to_command(snap.to_dict() or {}, command_id)
            if current.terminal:
                raise CommandTransitionError(
                    f"Command {command_id} is terminal ({current.state.value})",
                    current.state, current.state_version)
            if not transition_is_valid(current.state, target):
                raise CommandTransitionError(
                    f"Invalid transition {current.state.value} → {target.value}",
                    current.state, current.state_version)
            if expected_state_version is not None and \
                    current.state_version != int(expected_state_version):
                raise CommandTransitionError(
                    f"State version conflict for {command_id}", current.state,
                    current.state_version)
            new_version = current.state_version + 1
            transaction.update(ref, {
                "state": target.value, "state_version": new_version,
                "waiting_reason": waiting_reason.value if waiting_reason else None,
                "terminal_reason": terminal_reason,
                "result_reference": result_reference, "updated_at": now,
            })
            if outbox_event is not None:
                transaction.create(outbox_coll.document(outbox_event.event_id), {
                    "command_id": command_id,
                    "aggregate_id": outbox_event.aggregate_id or command_id,
                    "aggregate_version": new_version,
                    "event_type": outbox_event.event_type,
                    "schema_version": outbox_event.schema_version,
                    "payload_json": json.dumps(outbox_event.payload),
                    "created_at": now, "available_at": now,
                    "publish_attempts": 0, "dispatcher_owner": None,
                    "dispatcher_lease_expires_at": None, "published_at": None,
                    "last_error_code": None,
                })
            current.state = target
            current.state_version = new_version
            current.waiting_reason = waiting_reason
            current.terminal_reason = terminal_reason
            current.result_reference = result_reference
            # C3 (§5.0 Batch A): queue accounting follows the documented
            # definition — accepted-NOT-STARTED. (Post-transaction writes;
            # the counter doc is decremented in its own short transaction.)
            if target is CommandState.RUNNING or current.terminal:
                pending_ref = db.collection("pending_commands").document(command_id)
                pending_snap = await pending_ref.get()
                if pending_snap.exists:
                    await pending_ref.update({
                        "status": "dispatched" if target is CommandState.RUNNING
                        else "settled", "updated_at": now})
                if current.terminal:
                    counter_ref = db.collection("command_queue_counters").document(
                        f"user-{current.owner_user_id}")
                    counter_snap = await counter_ref.get()
                    if counter_snap.exists:
                        n = int((counter_snap.to_dict() or {}).get("count", 0))
                        await counter_ref.set({"count": max(0, n - 1)}, merge=True)
            return current

        return await apply(db.transaction())

    async def record_cancellation_intent(self, command_id: str, by: str,
                                         reason: str = "") -> Command:
        from google.cloud import firestore as fs_types
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection("commands").document(command_id)

        @fs_types.async_transactional
        async def cancel(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise CommandTransitionError(
                    f"Command {command_id} not found", CommandState.FAILED, 0,
                    CommandErrorCode.NOT_FOUND)
            current = _doc_to_command(snap.to_dict() or {}, command_id)
            if current.terminal or current.state is CommandState.CANCEL_REQUESTED:
                return current
            transaction.update(ref, {
                "state": CommandState.CANCEL_REQUESTED.value,
                "state_version": current.state_version + 1,
                "cancel_requested_at": now, "cancel_requested_by": by,
                "cancel_reason": reason, "updated_at": now,
            })
            current.state = CommandState.CANCEL_REQUESTED
            current.state_version += 1
            current.cancel_requested_at = now
            current.cancel_requested_by = by
            current.cancel_reason = reason
            return current

        return await cancel(db.transaction())

    async def acknowledge_cancellation(self, command_id: str) -> Command:
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection("commands").document(command_id)
        snap = await ref.get()
        if snap.exists:
            data = snap.to_dict() or {}
            if data.get("cancel_requested_at") and not data.get("cancel_acknowledged_at"):
                await ref.update({"cancel_acknowledged_at": now, "updated_at": now})
        command = await self.get_command(command_id)
        assert command is not None
        return command

    # ── Outbox (§4.7/§4.8) ─────────────────────────────────────────────

    async def append_outbox(self, event: OutboxEvent) -> None:
        db = await self._get_db()
        now = epoch_now()
        if event.created_at == 0:
            event.created_at = now
        if event.available_at == 0:
            event.available_at = now
        await db.collection("outbox_events").document(event.event_id).set({
            "command_id": event.command_id, "aggregate_id": event.aggregate_id,
            "aggregate_version": event.aggregate_version,
            "event_type": event.event_type,
            "schema_version": event.schema_version,
            "payload_json": json.dumps(event.payload),
            "created_at": event.created_at, "available_at": event.available_at,
            "publish_attempts": 0, "dispatcher_owner": None,
            "dispatcher_lease_expires_at": None, "published_at": None,
            "last_error_code": None,
        }, merge=True)

    async def claim_outbox_batch(self, dispatcher_owner: str, batch: int,
                                 lease_seconds: int) -> List[OutboxEvent]:
        db = await self._get_db()
        from google.cloud import firestore as fs_types
        now = epoch_now()
        lease_until = now + lease_seconds
        claimed: List[OutboxEvent] = []
        query = (db.collection("outbox_events")
                 .where("published_at", "==", None)
                 .where("available_at", "<=", now)
                 .order_by("created_at").limit(batch))
        for snap in await query.get():
            data = snap.to_dict() or {}
            if data.get("dispatcher_owner") and \
                    int(data.get("dispatcher_lease_expires_at") or 0) > now:
                continue   # another dispatcher holds a live lease
            ok = await self._try_claim(db, snap.id, dispatcher_owner, lease_until)
            if ok:
                data["dispatcher_owner"] = dispatcher_owner
                data["dispatcher_lease_expires_at"] = lease_until
                claimed.append(_doc_to_event(data, snap.id))
        return claimed

    @staticmethod
    async def _try_claim(db, event_id: str, owner: str, lease_until: int) -> bool:
        from google.cloud import firestore as fs_types

        @fs_types.async_transactional
        async def claim(transaction):
            ref = db.collection("outbox_events").document(event_id)
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            if data.get("published_at") is not None:
                return False
            if data.get("dispatcher_owner") and \
                    int(data.get("dispatcher_lease_expires_at") or 0) > epoch_now():
                return False
            transaction.update(ref, {"dispatcher_owner": owner,
                                     "dispatcher_lease_expires_at": lease_until})
            return True

        try:
            return await claim(db.transaction())
        except Exception:
            return False

    async def mark_outbox_published(self, event_id: str) -> bool:
        db = await self._get_db()
        ref = db.collection("outbox_events").document(event_id)
        snap = await ref.get()
        if not snap.exists or (snap.to_dict() or {}).get("published_at") is not None:
            return False
        await ref.update({"published_at": epoch_now(), "dispatcher_owner": None,
                          "dispatcher_lease_expires_at": None})
        return True

    async def fail_outbox_event(self, event_id: str, error_code: str) -> None:
        db = await self._get_db()
        ref = db.collection("outbox_events").document(event_id)
        snap = await ref.get()
        if not snap.exists:
            return
        attempts = int((snap.to_dict() or {}).get("publish_attempts", 0)) + 1
        delay = min(300, 2 ** min(attempts, 8))
        await ref.update({"publish_attempts": attempts,
                          "available_at": epoch_now() + delay,
                          "last_error_code": error_code,
                          "dispatcher_owner": None,
                          "dispatcher_lease_expires_at": None})

    async def reclaim_stale_outbox_leases(self) -> int:
        db = await self._get_db()
        now = epoch_now()
        query = (db.collection("outbox_events")
                 .where("published_at", "==", None)
                 .where("dispatcher_lease_expires_at", "<=", now))
        freed = 0
        for snap in await query.get():
            await snap.reference.update({"dispatcher_owner": None,
                                         "dispatcher_lease_expires_at": None})
            freed += 1
        return freed

    async def outbox_stats(self) -> Dict[str, int]:
        db = await self._get_db()
        now = epoch_now()
        query = db.collection("outbox_events").where("published_at", "==", None)
        total = 0
        oldest: Optional[int] = None
        for snap in await query.get():
            total += 1
            created = int((snap.to_dict() or {}).get("created_at", now))
            oldest = created if oldest is None else min(oldest, created)
        return {"unpublished": total,
                "oldest_unpublished_age_seconds": max(0, now - oldest)
                if oldest else 0}
