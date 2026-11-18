"""Capacity-lease repositories (Phase 3 §3.11).

Durable counters of ACTIVE work per (resource_class, tenant scope) with
lease TTLs and fencing generations. A lease is the unit of execution
concurrency admission — request-rate counters (shared_rate_limiter) cap how
often work STARTS; leases cap how much work RUNS AT ONCE.

Required properties (§3.11), each enforced atomically in the store:
1. Acquisition is atomic (guarded single INSERT; two workers cannot both win
   the last unit).
2. Repeated acquisition for the SAME execution does not allocate twice
   (partial UNIQUE index on (resource_class, scope_key, execution_key) WHERE
   state='active' — the IntegrityError is translated into idempotent return
   of the existing lease).
3. Release is idempotent (releasing a released lease is a no-op success).
4. Renewal verifies current ownership (worker + fencing generation must
   match an unexpired active lease).
5. Stale workers cannot release a newer worker's lease (fencing generation:
   each acquisition for an execution key increments generation; a worker
   holding generation N loses all rights the moment N+1 exists).
6. Expiration reclaims capacity from crashed workers (TTL sweep on acquire
   and via reconcile_expired from the worker ticker).

§3.11's warning stands: expiration alone cannot PROVE the old browser or
provider call stopped — a partitioned worker may still be running. TTL
recovery therefore increments the fencing generation, and any late
renew/release/commit from the zombie is rejected; strict physical bounds
(e.g. browser hard-kills) remain an operational concern, documented in
docs/admission-and-capacity.md.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional, Protocol

from backend.models.admission import (
    CapacityLease,
    CapacityError,
    CapacityExhaustedError,
    LeaseConflictError,
    LeaseState,
    ResourceClass,
    epoch_now,
)

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 15 * 60
MAX_EXECUTION_KEY_LENGTH = 300
MAX_SCOPE_KEY_LENGTH = 200


def _validate(resource_class: ResourceClass, scope_key: str, execution_key: str,
              units: int) -> None:
    if not scope_key or not scope_key.strip():
        raise CapacityError("Capacity scope_key must be a non-empty tenant identity")
    if len(scope_key) > MAX_SCOPE_KEY_LENGTH:
        raise CapacityError(f"Capacity scope_key exceeds {MAX_SCOPE_KEY_LENGTH} chars")
    if not execution_key or not execution_key.strip():
        raise CapacityError("Capacity execution_key must be non-empty (idempotency key)")
    if len(execution_key) > MAX_EXECUTION_KEY_LENGTH:
        raise CapacityError(f"Capacity execution_key exceeds {MAX_EXECUTION_KEY_LENGTH} chars")
    if int(units) <= 0:
        raise CapacityError(f"Capacity units must be > 0 (got {units})")


def deterministic_lease_id(resource_class: ResourceClass, scope_key: str,
                           execution_key: str, generation: int) -> str:
    raw = f"{resource_class.value}|{scope_key}|{execution_key}|{int(generation)}"
    return "cl_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


class AcquireOutcome:
    """Acquisition result with an EXPLICIT ownership verdict (§4.10, R11):
    deduplication must never transfer ownership. `lease` is populated ONLY
    when this caller may execute under it (created, or already owned by the
    SAME worker). A foreign holder's lease is never handed out."""

    ACQUIRED_BY_THIS_WORKER = "ACQUIRED_BY_THIS_WORKER"
    ALREADY_OWNED_BY_THIS_WORKER = "ALREADY_OWNED_BY_THIS_WORKER"
    OWNED_BY_ANOTHER_WORKER = "OWNED_BY_ANOTHER_WORKER"

    def __init__(self, lease: Optional[CapacityLease], created: bool,
                 outcome: str):
        self._lease = lease
        self.created = created
        self.outcome = outcome

    @property
    def lease(self) -> CapacityLease:
        """The lease handle — ONLY when this caller may execute under it.
        §4.10 (R11): a foreign holder's lease is never handed out; touching
        it is a programming error and raises."""
        if not self.authorized:
            raise CapacityError(
                f"Lease not authorized for this worker (outcome={self.outcome}): "
                "deduplication never transfers ownership")
        return self._lease

    @property
    def authorized(self) -> bool:
        """True only when THIS worker may execute under the lease."""
        return self.outcome in (self.ACQUIRED_BY_THIS_WORKER,
                                self.ALREADY_OWNED_BY_THIS_WORKER)


class CapacityRepository(Protocol):
    async def acquire(self, resource_class: ResourceClass, scope_key: str,
                      owner_id: str, execution_key: str, limit: int,
                      units: int = 1, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                      worker_id: str = "", organization_id: Optional[str] = None,
                      command_id: Optional[str] = None, task_id: Optional[str] = None,
                      attempt_id: Optional[str] = None) -> AcquireOutcome: ...

    async def renew(self, lease_id: str, worker_id: str, generation: int,
                    extend_seconds: int) -> CapacityLease: ...

    async def release(self, lease_id: str, worker_id: str, generation: int) -> bool: ...

    async def release_execution(self, resource_class: ResourceClass, scope_key: str,
                                execution_key: str) -> bool:
        """Release the ACTIVE lease for a logical execution (same execution
        key = same logical work, e.g. a resumed browser session closing the
        slot its original run acquired). Idempotent."""
        ...

    async def reconcile_expired(self, now_epoch: Optional[int] = None) -> int: ...

    async def active_units(self, resource_class: ResourceClass,
                           scope_key: str) -> int: ...

    async def get_lease(self, lease_id: str) -> Optional[CapacityLease]: ...


def _row_to_lease(row) -> CapacityLease:
    return CapacityLease(
        lease_id=row["lease_id"],
        resource_class=ResourceClass(row["resource_class"]),
        scope_key=row["scope_key"],
        owner_id=row["owner_id"],
        execution_key=row["execution_key"],
        worker_id=row["worker_id"] or "",
        units=int(row["units"]),
        generation=int(row["generation"]),
        acquired_at_epoch=int(row["acquired_at"]),
        expires_at_epoch=int(row["expires_at"]),
        last_heartbeat_epoch=int(row["last_heartbeat"]),
        state=LeaseState(row["state"]),
        organization_id=row["organization_id"],
        command_id=row["command_id"],
        task_id=row["task_id"],
        attempt_id=row["attempt_id"],
    )


class SQLiteCapacityRepository:
    """Single-statement guarded acquisition on the shared SQLite store; the
    partial unique index makes duplicate-execution races impossible."""

    def __init__(self, db):
        self.db = db

    async def _sweep_scope(self, resource_class: ResourceClass, scope_key: str,
                           now: int) -> None:
        await self.db.execute(
            "UPDATE capacity_leases SET state='expired' "
            "WHERE resource_class=? AND scope_key=? AND state='active' AND expires_at<=?",
            (resource_class.value, scope_key, now),
        )
        await self.db.commit()

    @staticmethod
    def _outcome_for(existing: CapacityLease, worker_id: str) -> AcquireOutcome:
        """§4.10 (R11): deduplication must not transfer ownership. The SAME
        worker re-entering gets its lease back (idempotent); a DIFFERENT
        worker gets OWNED_BY_ANOTHER_WORKER with NO lease handle."""
        # Equal identities (including anonymous '' local-mode callers) are
        # the SAME worker re-entering; production workers carry unique
        # instance identities (§4.10 R12) so '' collisions cannot occur there.
        if (existing.worker_id or "") == (worker_id or ""):
            return AcquireOutcome(existing, created=False,
                                  outcome=AcquireOutcome.ALREADY_OWNED_BY_THIS_WORKER)
        return AcquireOutcome(None, created=False,
                              outcome=AcquireOutcome.OWNED_BY_ANOTHER_WORKER)

    async def _find_active(self, resource_class: ResourceClass, scope_key: str,
                           execution_key: str, now: int) -> Optional[CapacityLease]:
        cursor = await self.db.execute(
            "SELECT * FROM capacity_leases WHERE resource_class=? AND scope_key=? "
            "AND execution_key=? AND state='active' AND expires_at>? LIMIT 1",
            (resource_class.value, scope_key, execution_key, now),
        )
        row = await cursor.fetchone()
        return _row_to_lease(row) if row else None

    async def acquire(self, resource_class: ResourceClass, scope_key: str,
                      owner_id: str, execution_key: str, limit: int,
                      units: int = 1, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                      worker_id: str = "", organization_id: Optional[str] = None,
                      command_id: Optional[str] = None, task_id: Optional[str] = None,
                      attempt_id: Optional[str] = None) -> AcquireOutcome:
        _validate(resource_class, scope_key, execution_key, units)
        if int(limit) <= 0:
            raise CapacityError(f"Capacity limit for {resource_class.value} must be > 0")
        now = epoch_now()
        expires = now + max(30, int(ttl_seconds))

        # Free capacity held by leases whose TTL passed (crashed workers).
        await self._sweep_scope(resource_class, scope_key, now)

        # Next fencing generation for this execution key.
        cursor = await self.db.execute(
            "SELECT COALESCE(MAX(generation), 0) FROM capacity_leases "
            "WHERE resource_class=? AND scope_key=? AND execution_key=?",
            (resource_class.value, scope_key, execution_key),
        )
        generation = int((await cursor.fetchone())[0]) + 1
        lease_id = deterministic_lease_id(resource_class, scope_key, execution_key, generation)

        try:
            cursor = await self.db.execute(
                """
                INSERT INTO capacity_leases
                    (lease_id, resource_class, scope_key, owner_id, execution_key,
                     worker_id, units, generation, acquired_at, expires_at,
                     last_heartbeat, state, organization_id, command_id, task_id, attempt_id)
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?
                WHERE
                    (SELECT COALESCE(SUM(units), 0) FROM capacity_leases
                     WHERE resource_class=? AND scope_key=? AND state='active'
                       AND expires_at>?) + ? <= ?
                """,
                (lease_id, resource_class.value, scope_key, owner_id, execution_key,
                 worker_id, int(units), generation, now, expires, now,
                 organization_id, command_id, task_id, attempt_id,
                 resource_class.value, scope_key, now, int(units), int(limit)),
            )
            await self.db.commit()
        except Exception as exc:
            # The partial unique index fired: the capacity for THIS execution
            # key is already held. §4.10 (R11): resolve the outcome by
            # OWNERSHIP — same worker may re-enter idempotently; a foreign
            # holder's lease is NEVER handed out as permission.
            existing = await self._find_active(resource_class, scope_key, execution_key, now)
            if existing is not None:
                return self._outcome_for(existing, worker_id)
            raise exc

        if cursor.rowcount and cursor.rowcount > 0:
            lease = await self.get_lease(lease_id)
            return AcquireOutcome(lease, created=True,
                                  outcome=AcquireOutcome.ACQUIRED_BY_THIS_WORKER)

        # Guard rejected: either the tenant cap is exhausted, or an execution
        # lease appeared between the sweep and the insert.
        existing = await self._find_active(resource_class, scope_key, execution_key, now)
        if existing is not None:
            return self._outcome_for(existing, worker_id)
        in_use = await self.active_units(resource_class, scope_key)
        raise CapacityExhaustedError(
            f"Capacity exhausted for {resource_class.value} scope {scope_key}: "
            f"{in_use}/{limit} units active") from None

    async def renew(self, lease_id: str, worker_id: str, generation: int,
                    extend_seconds: int) -> CapacityLease:
        """§3.11/§4.10: renewal verifies CURRENT ownership — the caller's
        worker_id must EQUAL the stored worker_id (an empty stored id only
        matches an empty caller id, never an arbitrary identity) and the
        generation must be current."""
        now = epoch_now()
        cursor = await self.db.execute(
            "UPDATE capacity_leases SET expires_at=?, last_heartbeat=? "
            "WHERE lease_id=? AND state='active' AND expires_at>? AND generation=? "
            "AND worker_id=?",
            (now + max(30, int(extend_seconds)), now, lease_id, now, int(generation), worker_id),
        )
        await self.db.commit()
        if cursor.rowcount and cursor.rowcount > 0:
            lease = await self.get_lease(lease_id)
            return lease
        lease = await self.get_lease(lease_id)
        if lease is None:
            raise CapacityError(f"Lease {lease_id} not found")
        _raise_for_stale_lease(lease, generation)

    async def release(self, lease_id: str, worker_id: str, generation: int) -> bool:
        # §4.10 (R11): release requires the CURRENT owner's identity AND
        # generation — same strict equality as renew.
        cursor = await self.db.execute(
            "UPDATE capacity_leases SET state='released' "
            "WHERE lease_id=? AND state='active' AND generation=? "
            "AND worker_id=?",
            (lease_id, int(generation), worker_id),
        )
        await self.db.commit()
        if cursor.rowcount and cursor.rowcount > 0:
            return True
        lease = await self.get_lease(lease_id)
        if lease is None:
            raise CapacityError(f"Lease {lease_id} not found")
        if lease.state in (LeaseState.RELEASED, LeaseState.ORPHANED, LeaseState.EXPIRED):
            return True   # idempotent; expired capacity was already reclaimed
        _raise_for_stale_lease(lease, generation)

    async def release_execution(self, resource_class: ResourceClass, scope_key: str,
                                execution_key: str) -> bool:
        cursor = await self.db.execute(
            "UPDATE capacity_leases SET state='released' "
            "WHERE resource_class=? AND scope_key=? AND execution_key=? AND state='active'",
            (resource_class.value, scope_key, execution_key),
        )
        await self.db.commit()
        return bool(cursor.rowcount)

    async def reconcile_expired(self, now_epoch: Optional[int] = None) -> int:
        """Bounded sweep of expired ACTIVE leases (worker ticker + acquire)."""
        now = int(now_epoch if now_epoch is not None else epoch_now())
        total = 0
        for _ in range(20):  # ≤ 10k per sweep
            cursor = await self.db.execute(
                "UPDATE capacity_leases SET state='expired' WHERE rowid IN ("
                "  SELECT rowid FROM capacity_leases WHERE state='active' "
                "  AND expires_at<=? LIMIT 500)",
                (now,),
            )
            await self.db.commit()
            swept = cursor.rowcount or 0
            total += swept
            if swept < 500:
                break
        return total

    async def active_units(self, resource_class: ResourceClass, scope_key: str) -> int:
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT COALESCE(SUM(units), 0) FROM capacity_leases "
            "WHERE resource_class=? AND scope_key=? AND state='active' AND expires_at>?",
            (resource_class.value, scope_key, now),
        )
        return int((await cursor.fetchone())[0])

    async def active_units_by_class(self) -> dict:
        """§3.20 observability: ACTIVE lease units per resource class
        (platform-wide). Cardinality is bounded by the enum, never tenants."""
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT resource_class, COALESCE(SUM(units), 0) FROM capacity_leases "
            "WHERE state='active' AND expires_at>? GROUP BY resource_class",
            (now,),
        )
        return {row[0]: int(row[1]) for row in await cursor.fetchall()}

    async def get_lease(self, lease_id: str) -> Optional[CapacityLease]:
        cursor = await self.db.execute(
            "SELECT * FROM capacity_leases WHERE lease_id=?", (lease_id,))
        row = await cursor.fetchone()
        return _row_to_lease(row) if row else None


def _raise_for_stale_lease(lease: CapacityLease, generation: int) -> None:
    if lease.generation != int(generation):
        raise LeaseConflictError(
            f"Fenced out: lease {lease.lease_id} is at generation {lease.generation}, "
            f"caller held {generation} — a newer execution owns this capacity")
    raise CapacityError(
        f"Lease {lease.lease_id} is {lease.state.value} and cannot be managed")


class FirestoreCapacityRepository:
    """Transaction-based lease admission. Equality-only filters, so Firestore
    serves the queries from merged single-field indexes."""

    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def acquire(self, resource_class: ResourceClass, scope_key: str,
                      owner_id: str, execution_key: str, limit: int,
                      units: int = 1, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                      worker_id: str = "", organization_id: Optional[str] = None,
                      command_id: Optional[str] = None, task_id: Optional[str] = None,
                      attempt_id: Optional[str] = None) -> AcquireOutcome:
        from google.cloud import firestore
        _validate(resource_class, scope_key, execution_key, units)
        if int(limit) <= 0:
            raise CapacityError(f"Capacity limit for {resource_class.value} must be > 0")
        db = await self._get_db()
        now = epoch_now()
        expires = now + max(30, int(ttl_seconds))
        coll = db.collection("capacity_leases")

        @firestore.async_transactional
        async def admit(transaction):
            query = (coll.where("resource_class", "==", resource_class.value)
                     .where("scope_key", "==", scope_key)
                     .where("state", "==", "active"))
            docs = [snap async for snap in query.stream(transaction=transaction)]
            active = []
            mine = None
            max_generation = 0
            for snap in docs:
                data = snap.to_dict() or {}
                if int(data.get("expires_at", 0)) <= now:
                    transaction.update(snap.reference, {"state": "expired"})
                    continue
                active.append(data)
                if data.get("execution_key") == execution_key:
                    mine = data
                max_generation = max(max_generation, int(data.get("generation", 1)))
            # §4.7: DURABLE generation — the counter doc endures after leases
            # expire, so ownership generations can never repeat accidentally
            # (the active-lease MAX alone would recycle them).
            gen_ref = db.collection("capacity_generations").document(
                f"{resource_class.value}|{scope_key}|{execution_key}")
            gen_snap = await gen_ref.get(transaction=transaction)
            durable_generation = int((gen_snap.to_dict() or {}).get("generation", 0))
            max_generation = max(max_generation, durable_generation)
            if mine is not None:
                mine_lease = _fs_to_lease(mine, mine.get("lease_id"))
                # Parity with the SQLite contract (_outcome_for): empty
                # worker ids compare equal — the duplicate-key decision
                # must not depend on whether callers pass worker ids.
                if (mine_lease.worker_id or "") == (worker_id or ""):
                    return AcquireOutcome(mine_lease, created=False,
                                          outcome=AcquireOutcome.ALREADY_OWNED_BY_THIS_WORKER)
                return AcquireOutcome(None, created=False,
                                      outcome=AcquireOutcome.OWNED_BY_ANOTHER_WORKER)
            in_use = sum(int(d.get("units", 1)) for d in active)
            if in_use + int(units) > int(limit):
                raise CapacityExhaustedError(
                    f"Capacity exhausted for {resource_class.value} scope {scope_key}: "
                    f"{in_use}/{limit} units active")
            lease_id = deterministic_lease_id(
                resource_class, scope_key, execution_key, max_generation + 1)
            record = {
                "lease_id": lease_id, "resource_class": resource_class.value,
                "scope_key": scope_key, "owner_id": owner_id,
                "execution_key": execution_key, "worker_id": worker_id,
                "units": int(units), "generation": max_generation + 1,
                "acquired_at": now, "expires_at": expires, "last_heartbeat": now,
                "state": "active", "organization_id": organization_id,
                "command_id": command_id, "task_id": task_id, "attempt_id": attempt_id,
            }
            transaction.set(coll.document(lease_id), record)
            transaction.set(gen_ref, {"generation": max_generation + 1},
                            merge=True)
            return AcquireOutcome(_fs_to_lease(record, lease_id), created=True,
                                  outcome=AcquireOutcome.ACQUIRED_BY_THIS_WORKER)

        return await admit(db.transaction())

    async def renew(self, lease_id: str, worker_id: str, generation: int,
                    extend_seconds: int) -> CapacityLease:
        from google.cloud import firestore
        db = await self._get_db()
        ref = db.collection("capacity_leases").document(lease_id)
        now = epoch_now()

        @firestore.async_transactional
        async def renew_tx(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise CapacityError(f"Lease {lease_id} not found")
            data = snap.to_dict() or {}
            lease = _fs_to_lease(data, lease_id)
            _verify_renewable(lease, worker_id, generation, now)
            transaction.update(ref, {"expires_at": now + max(30, int(extend_seconds)),
                                     "last_heartbeat": now})
            lease.expires_at_epoch = now + max(30, int(extend_seconds))
            lease.last_heartbeat_epoch = now
            return lease

        return await renew_tx(db.transaction())

    async def release(self, lease_id: str, worker_id: str, generation: int) -> bool:
        from google.cloud import firestore
        db = await self._get_db()
        ref = db.collection("capacity_leases").document(lease_id)

        @firestore.async_transactional
        async def release_tx(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise CapacityError(f"Lease {lease_id} not found")
            lease = _fs_to_lease(snap.to_dict() or {}, lease_id)
            if lease.state in (LeaseState.RELEASED, LeaseState.ORPHANED, LeaseState.EXPIRED):
                return True   # idempotent
            if lease.generation != int(generation):
                raise LeaseConflictError(
                    f"Fenced out: lease {lease_id} is at generation {lease.generation}, "
                    f"caller held {generation}")
            if (lease.worker_id or "") != (worker_id or ""):
                raise LeaseConflictError(
                    f"Lease {lease_id} is owned by {lease.worker_id}; "
                    f"caller {worker_id} cannot release it")
            transaction.update(ref, {"state": "released"})
            return True

        return await release_tx(db.transaction())

    async def release_execution(self, resource_class: ResourceClass, scope_key: str,
                                execution_key: str) -> bool:
        from google.cloud import firestore
        db = await self._get_db()
        query = (db.collection("capacity_leases")
                 .where("resource_class", "==", resource_class.value)
                 .where("scope_key", "==", scope_key)
                 .where("execution_key", "==", execution_key)
                 .where("state", "==", "active"))
        released = False
        async for snap in query.stream():
            await snap.reference.update({"state": "released"})
            released = True
        return released

    async def reconcile_expired(self, now_epoch: Optional[int] = None) -> int:
        db = await self._get_db()
        now = int(now_epoch if now_epoch is not None else epoch_now())
        swept = 0
        while True:
            query = (db.collection("capacity_leases")
                     .where("state", "==", "active")
                     .where("expires_at", "<=", now)
                     .limit(500))
            snaps = [snap async for snap in query.stream()]
            if not snaps:
                break
            for snap in snaps:
                await snap.reference.update({"state": "expired"})
                swept += 1
            if len(snaps) < 500:
                break
        return swept

    async def active_units(self, resource_class: ResourceClass, scope_key: str) -> int:
        db = await self._get_db()
        now = epoch_now()
        query = (db.collection("capacity_leases")
                 .where("resource_class", "==", resource_class.value)
                 .where("scope_key", "==", scope_key)
                 .where("state", "==", "active"))
        total = 0
        async for snap in query.stream():
            data = snap.to_dict() or {}
            if int(data.get("expires_at", 0)) > now:
                total += int(data.get("units", 1))
        return total

    async def active_units_by_class(self) -> dict:
        db = await self._get_db()
        now = epoch_now()
        query = db.collection("capacity_leases").where("state", "==", "active")
        totals: dict[str, int] = {}
        async for snap in query.stream():
            data = snap.to_dict() or {}
            if int(data.get("expires_at", 0)) > now:
                cls = data.get("resource_class", "unknown")
                totals[cls] = totals.get(cls, 0) + int(data.get("units", 1))
        return totals

    async def get_lease(self, lease_id: str) -> Optional[CapacityLease]:
        db = await self._get_db()
        snap = await db.collection("capacity_leases").document(lease_id).get()
        if not snap.exists:
            return None
        return _fs_to_lease(snap.to_dict() or {}, lease_id)


def _fs_to_lease(data: dict, lease_id: str) -> CapacityLease:
    return CapacityLease(
        lease_id=lease_id,
        resource_class=ResourceClass(data["resource_class"]),
        scope_key=data["scope_key"],
        owner_id=data.get("owner_id", ""),
        execution_key=data["execution_key"],
        worker_id=data.get("worker_id", "") or "",
        units=int(data.get("units", 1)),
        generation=int(data.get("generation", 1)),
        acquired_at_epoch=int(data.get("acquired_at", 0)),
        expires_at_epoch=int(data.get("expires_at", 0)),
        last_heartbeat_epoch=int(data.get("last_heartbeat", 0)),
        state=LeaseState(data.get("state", "active")),
        organization_id=data.get("organization_id"),
        command_id=data.get("command_id"),
        task_id=data.get("task_id"),
        attempt_id=data.get("attempt_id"),
    )


def _verify_renewable(lease: CapacityLease, worker_id: str, generation: int, now: int) -> None:
    if lease.generation != int(generation):
        raise LeaseConflictError(
            f"Fenced out: lease {lease.lease_id} is at generation {lease.generation}, "
            f"caller held {generation} — a newer execution owns this capacity")
    # §3.11/§4.10: renewal verifies CURRENT ownership — same strict
    # equality as the SQLite renew (an empty stored id only matches an
    # empty caller id).
    if (lease.worker_id or "") != (worker_id or ""):
        raise LeaseConflictError(
            f"Lease {lease.lease_id} is owned by '{lease.worker_id}'; "
            f"caller '{worker_id}' cannot renew it")
    if lease.state != LeaseState.ACTIVE:
        raise CapacityError(
            f"Lease {lease.lease_id} is {lease.state.value} and cannot be renewed")
    if lease.expires_at_epoch <= now:
        raise CapacityError(f"Lease {lease.lease_id} expired before renewal")


_repo: Optional[CapacityRepository] = None


def set_capacity_repo(repo: Optional[CapacityRepository]) -> None:
    global _repo
    _repo = repo


def get_capacity_repo() -> Optional[CapacityRepository]:
    return _repo
