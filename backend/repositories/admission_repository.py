"""Durable command-admission repository (Phase 3 §3.12).

The strict queue counter. Pub/Sub backlog metrics are approximations of how
much work a transport HOLDS; this table is the transactional record of how
much work the SERVICE has ACCEPTED and not yet settled. Per-tenant pending
caps, per-class caps, and maximum-wait expiry are checked against it inside
the same transaction that inserts the admission row, so "accepted" can never
exceed "bounded".

One logical command = one row (PRIMARY KEY command_id; optional idempotency
key UNIQUE): a duplicate submission returns the EXISTING record instead of
consuming another queue slot (§3.10 command admission vs. request rate).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Protocol

from backend.models.admission import (
    AdmissionReason,
    epoch_now,
)

MAX_COMMAND_WAIT_SECONDS = 6 * 3600   # §3.12: waiting work cannot outlive policy


@dataclass(frozen=True)
class CommandAdmission:
    """The typed answer to 'may this logical work join the queue?'"""
    accepted: bool
    command_id: str
    reason: AdmissionReason = AdmissionReason.ALLOWED
    duplicate: bool = False            # True → return existing, don't re-enqueue
    pending_for_user: int = 0
    pending_for_org: int = 0
    pending_for_class: int = 0
    expires_at_epoch: int = 0
    first_command_id: str = ""         # populated when duplicate=True

    @property
    def retry_after_seconds(self) -> int:
        if self.reason == AdmissionReason.QUEUE_FULL:
            return 30
        if self.reason == AdmissionReason.ADMISSION_UNAVAILABLE:
            return 5
        return 0


class CommandAdmissionRepository(Protocol):
    async def admit_command(self, command_id: str, user_id: str,
                            org_id: Optional[str], workload_class: str,
                            max_pending_per_user: int,
                            max_pending_per_org: int,
                            max_pending_per_class: int,
                            max_wait_seconds: int,
                            idempotency_key: Optional[str] = None,
                            request_digest: Optional[str] = None) -> CommandAdmission: ...

    async def mark_dispatched(self, command_id: str) -> None: ...

    async def settle_command(self, command_id: str) -> None: ...

    async def expire_stale(self, now_epoch: Optional[int] = None) -> int: ...

    async def pending_counts(self, user_id: Optional[str] = None,
                             org_id: Optional[str] = None,
                             workload_class: Optional[str] = None) -> int: ...

    async def get_command(self, command_id: str) -> Optional[dict]: ...


class SQLiteCommandAdmissionRepository:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _scoped_key(user_id: str, workload_class: str,
                    idempotency_key: Optional[str]) -> Optional[str]:
        """§4.0D (R8): idempotency keys are TENANT-SCOPED — the same raw key
        from another tenant (or another operation) is an independent command.
        The stored value is globally unique; the scope prefix makes it so."""
        if not idempotency_key:
            return None
        return f"{user_id}|{workload_class}|{idempotency_key}"

    async def admit_command(self, command_id: str, user_id: str,
                            org_id: Optional[str], workload_class: str,
                            max_pending_per_user: int,
                            max_pending_per_org: int,
                            max_pending_per_class: int,
                            max_wait_seconds: int,
                            idempotency_key: Optional[str] = None,
                            request_digest: Optional[str] = None) -> CommandAdmission:
        """§4.0D (R7): the WHOLE read-check-insert sequence runs inside ONE
        `BEGIN IMMEDIATE` transaction on a dedicated connection — the write
        lock is acquired BEFORE the idempotency lookup and cap counts, so two
        independent connections/processes racing for the last slot cannot
        both pass check-then-insert. SQLITE_BUSY is bounded (busy_timeout +
        BEGIN retry), never an unbounded loop."""
        if not command_id or not command_id.strip():
            raise ValueError("command_id must be a non-empty string")
        if not user_id or not user_id.strip():
            raise ValueError("user_id must be a non-empty string")
        if not workload_class or not workload_class.strip():
            raise ValueError("workload_class must be a non-empty string")
        now = epoch_now()
        max_wait = min(max(1, int(max_wait_seconds)), MAX_COMMAND_WAIT_SECONDS)
        expires = now + max_wait
        scoped = self._scoped_key(user_id, workload_class, idempotency_key)

        async with self.db.write_transaction() as conn:
            # ── Idempotency: scoped key + payload digest (R8) ────────────
            if scoped:
                cursor = await conn.execute(
                    "SELECT command_id, request_digest FROM pending_commands "
                    "WHERE idempotency_key=? AND status IN ('pending','dispatched')",
                    (scoped,))
                row = await cursor.fetchone()
                if row:
                    existing_digest = row["request_digest"]
                    if (request_digest is not None and existing_digest
                            and existing_digest != request_digest):
                        return CommandAdmission(
                            False, command_id,
                            reason=AdmissionReason.IDEMPOTENCY_CONFLICT,
                            first_command_id=row["command_id"])
                    return CommandAdmission(True, row["command_id"],
                                            duplicate=True,
                                            first_command_id=row["command_id"])
                # A settled/expired/rejected holder frees its scoped key —
                # the UNIQUE index guards only unsettled work.
                await conn.execute(
                    "DELETE FROM pending_commands "
                    "WHERE idempotency_key=? AND status NOT IN ('pending','dispatched')",
                    (scoped,))

            cursor = await conn.execute(
                "SELECT command_id, status FROM pending_commands WHERE command_id=?",
                (command_id,))
            row = await cursor.fetchone()
            if row and row["status"] in ("pending", "dispatched"):
                return CommandAdmission(True, command_id, duplicate=True,
                                        first_command_id=row["command_id"])

            # ── Caps: read under the SAME write lock that guards the insert ──
            cursor = await conn.execute(
                "UPDATE pending_commands SET status='expired', updated_at=? "
                "WHERE status IN ('pending','dispatched') AND expires_at<=?",
                (now, now))
            for scope_sql, scope_args, cap in (
                    ("user_id=?", (user_id,), max_pending_per_user),
                    ("org_id=?", (org_id,), max_pending_per_org),
                    ("workload_class=?", (workload_class,), max_pending_per_class)):
                if cap <= 0 or scope_args[0] is None:
                    continue
                c = await conn.execute(
                    "SELECT COUNT(*) FROM pending_commands "
                    "WHERE status IN ('pending','dispatched') AND " + scope_sql,
                    scope_args)
                pending = int((await c.fetchone())[0])
                if pending >= cap:
                    return CommandAdmission(False, command_id,
                                            reason=AdmissionReason.QUEUE_FULL,
                                            pending_for_user=pending)

            try:
                await conn.execute(
                    "INSERT INTO pending_commands (command_id, user_id, org_id, "
                    "workload_class, status, idempotency_key, request_digest, "
                    "created_at, expires_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                    (command_id, user_id, org_id, workload_class, scoped,
                     request_digest, now, expires, now))
            except Exception as exc:
                # A concurrent replica won the INSERT after our BEGIN
                # (e.g. Firestore-style retry semantics); resolve honestly.
                await conn.execute("ROLLBACK")
                if scoped:
                    cursor = await conn.execute(
                        "SELECT command_id, request_digest FROM pending_commands "
                        "WHERE idempotency_key=? AND status IN ('pending','dispatched')",
                        (scoped,))
                    row = await cursor.fetchone()
                    if row:
                        if (request_digest is not None and row["request_digest"]
                                and row["request_digest"] != request_digest):
                            return CommandAdmission(
                                False, command_id,
                                reason=AdmissionReason.IDEMPOTENCY_CONFLICT,
                                first_command_id=row["command_id"])
                        return CommandAdmission(True, command_id, duplicate=True,
                                                first_command_id=row["command_id"])
                cursor = await conn.execute(
                    "SELECT command_id FROM pending_commands WHERE command_id=?",
                    (command_id,))
                row = await cursor.fetchone()
                if row:
                    return CommandAdmission(True, command_id, duplicate=True,
                                            first_command_id=row["command_id"])
                raise exc
        counts = await self._counts(user_id, org_id, workload_class)
        return CommandAdmission(True, command_id, pending_for_user=counts[0],
                                pending_for_org=counts[1],
                                pending_for_class=counts[2],
                                expires_at_epoch=expires)

    async def _counts(self, user_id, org_id, workload_class):
        out = []
        for sql, args in (("user_id=?", (user_id,)),
                          ("org_id=?", (org_id,)),
                          ("workload_class=?", (workload_class,))):
            if args[0] is None:
                out.append(0)
                continue
            c = await self.db.execute(
                f"SELECT COUNT(*) FROM pending_commands WHERE status IN ('pending','dispatched') AND {sql}",
                args)
            out.append(int((await c.fetchone())[0]))
        return tuple(out)

    async def mark_dispatched(self, command_id: str) -> None:
        await self.db.execute(
            "UPDATE pending_commands SET status='dispatched', updated_at=? "
            "WHERE command_id=? AND status='pending'", (epoch_now(), command_id))
        await self.db.commit()

    async def settle_command(self, command_id: str) -> None:
        await self.db.execute(
            "UPDATE pending_commands SET status='settled', updated_at=? "
            "WHERE command_id=?", (epoch_now(), command_id))
        await self.db.commit()

    async def expire_stale(self, now_epoch: Optional[int] = None) -> int:
        now = int(now_epoch if now_epoch is not None else epoch_now())
        cursor = await self.db.execute(
            "UPDATE pending_commands SET status='expired', updated_at=? "
            "WHERE status IN ('pending','dispatched') AND expires_at<=?", (now, now))
        await self.db.commit()
        return cursor.rowcount or 0

    async def pending_counts(self, user_id=None, org_id=None,
                             workload_class=None) -> int:
        await self.expire_stale()
        counts = await self._counts(user_id, org_id, workload_class)
        return max(counts)

    async def pending_stats(self) -> dict:
        """§3.20 observability: pending commands per class and the OLDEST
        wait in seconds (bounded cardinality — per class, never per tenant)."""
        await self.expire_stale()
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT workload_class, COUNT(*) AS n, MIN(created_at) AS oldest "
            "FROM pending_commands WHERE status IN ('pending','dispatched') GROUP BY workload_class")
        rows = await cursor.fetchall()
        out = {}
        for row in rows:
            out[row["workload_class"]] = {
                "pending": int(row["n"]),
                "oldest_wait_seconds": max(0, now - int(row["oldest"])),
            }
        return out

    async def get_command(self, command_id: str) -> Optional[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM pending_commands WHERE command_id=?", (command_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


class FirestoreCommandAdmissionRepository:
    """Transaction-based admission: caps checked and row created inside ONE
    Firestore transaction, so two replicas racing the last slot resolve
    exactly one winner."""

    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def admit_command(self, command_id: str, user_id: str,
                            org_id: Optional[str], workload_class: str,
                            max_pending_per_user: int,
                            max_pending_per_org: int,
                            max_pending_per_class: int,
                            max_wait_seconds: int,
                            idempotency_key: Optional[str] = None,
                            request_digest: Optional[str] = None) -> CommandAdmission:
        from google.cloud import firestore as fs_types
        db = await self._get_db()
        now = epoch_now()
        max_wait = min(max(1, int(max_wait_seconds)), MAX_COMMAND_WAIT_SECONDS)
        expires = now + max_wait
        coll = db.collection("pending_commands")
        ref = coll.document(command_id)
        # §4.0D (R8): TENANT-SCOPED key — same raw key from another tenant or
        # operation is an independent command.
        scoped = SQLiteCommandAdmissionRepository._scoped_key(
            user_id, workload_class, idempotency_key)

        @fs_types.async_transactional
        async def admit(transaction):
            # Duplicate by scoped idempotency key (+ digest comparison).
            if scoped:
                query = (coll.where("idempotency_key", "==", scoped)
                         .where("status", "in", ["pending", "dispatched"]))
                async for snap in query.stream(transaction=transaction):
                    data = snap.to_dict() or {}
                    existing_digest = data.get("request_digest")
                    if (request_digest is not None and existing_digest
                            and existing_digest != request_digest):
                        return CommandAdmission(
                            False, command_id,
                            reason=AdmissionReason.IDEMPOTENCY_CONFLICT,
                            first_command_id=data.get("command_id", ""))
                    return CommandAdmission(True, data.get("command_id", command_id),
                                            duplicate=True,
                                            first_command_id=data.get("command_id", ""))
            snap = await ref.get(transaction=transaction)
            if snap.exists and (snap.to_dict() or {}).get("status") in ("pending", "dispatched"):
                return CommandAdmission(True, command_id, duplicate=True,
                                        first_command_id=command_id)
            for field, cap, scope_val in (("user_id", max_pending_per_user, user_id),
                                          ("org_id", max_pending_per_org, org_id),
                                          ("workload_class", max_pending_per_class,
                                           workload_class)):
                if cap <= 0 or scope_val is None:
                    continue
                query = coll.where(field, "==", scope_val).where("status", "in", ["pending", "dispatched"])
                pending = 0
                async for _ in query.stream(transaction=transaction):
                    pending += 1
                if pending >= cap:
                    return CommandAdmission(False, command_id,
                                            reason=AdmissionReason.QUEUE_FULL,
                                            pending_for_user=pending)
            transaction.create(ref, {
                "command_id": command_id, "user_id": user_id, "org_id": org_id,
                "workload_class": workload_class, "status": "pending",
                "idempotency_key": scoped, "request_digest": request_digest,
                "created_at": now,
                "expires_at": expires, "updated_at": now,
            })
            return CommandAdmission(True, command_id, expires_at_epoch=expires)

        return await admit(db.transaction())

    async def mark_dispatched(self, command_id: str) -> None:
        db = await self._get_db()
        await (db.collection("pending_commands").document(command_id)
               .update({"status": "dispatched", "updated_at": epoch_now()}))

    async def settle_command(self, command_id: str) -> None:
        db = await self._get_db()
        await (db.collection("pending_commands").document(command_id)
               .update({"status": "settled", "updated_at": epoch_now()}))

    async def expire_stale(self, now_epoch: Optional[int] = None) -> int:
        db = await self._get_db()
        now = int(now_epoch if now_epoch is not None else epoch_now())
        swept = 0
        while True:
            query = (db.collection("pending_commands")
                     .where("status", "in", ["pending", "dispatched"])
                     .where("expires_at", "<=", now).limit(500))
            snaps = [snap async for snap in query.stream()]
            if not snaps:
                break
            for snap in snaps:
                await snap.reference.update({"status": "expired",
                                            "updated_at": epoch_now()})
                swept += 1
            if len(snaps) < 500:
                break
        return swept

    async def pending_counts(self, user_id=None, org_id=None,
                             workload_class=None) -> int:
        db = await self._get_db()
        field, value = (("user_id", user_id) if user_id else
                        ("org_id", org_id) if org_id else
                        ("workload_class", workload_class))
        if value is None:
            return 0
        query = (db.collection("pending_commands")
                 .where(field, "==", value).where("status", "in", ["pending", "dispatched"]))
        total = 0
        async for _ in query.stream():
            total += 1
        return total

    async def pending_stats(self) -> dict:
        """§3.20: per-class pending counts + oldest wait (bounded labels)."""
        db = await self._get_db()
        await self.expire_stale()
        now = epoch_now()
        query = db.collection("pending_commands").where("status", "in", ["pending", "dispatched"])
        stats: dict[str, dict] = {}
        async for snap in query.stream():
            data = snap.to_dict() or {}
            cls = data.get("workload_class", "unknown")
            entry = stats.setdefault(cls, {"pending": 0, "oldest_wait_seconds": 0})
            entry["pending"] += 1
            created = int(data.get("created_at", now))
            entry["oldest_wait_seconds"] = max(entry["oldest_wait_seconds"], now - created)
        return stats

    async def get_command(self, command_id: str) -> Optional[dict]:
        db = await self._get_db()
        snap = await db.collection("pending_commands").document(command_id).get()
        return snap.to_dict() if snap.exists else None


_repo: Optional[CommandAdmissionRepository] = None


def set_command_admission_repo(repo: Optional[CommandAdmissionRepository]) -> None:
    global _repo
    _repo = repo


def get_command_admission_repo() -> Optional[CommandAdmissionRepository]:
    return _repo
