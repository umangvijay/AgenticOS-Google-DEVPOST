"""Durable budget accounting (Phase 2 — docs/budget-accounting.md).

Two layers live here:

1. The Phase 2 **reservation contract** (`UsageRepository` protocol): every
   paid provider attempt is admitted against a durable, individually
   identified reservation with atomic multi-scope admission, idempotent
   replay, optimistic state transitions, and an append-only event trail.
2. The **legacy daily-ledger surface** (`add_usage`/`get_usage`/…): kept for
   pre-Phase-2 callers and tests. Writes go through the same transactional
   bucket aggregates (with a `legacy_seed` mirror, §2.18) so migrated
   history counts exactly once.

SQLite operations run on dedicated connections inside BEGIN IMMEDIATE
(DatabaseManager.write_transaction) — the shared connection is never used
for accounting mutations, so unrelated work cannot interleave and no
repository method needs to commit on behalf of strangers.
"""
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, runtime_checkable

from backend.models.usage import (
    BudgetError,
    BudgetEvent,
    BudgetEventType,
    BudgetErrorCode,
    BudgetScope,
    BudgetSnapshot,
    BudgetUnit,
    MeasurementSource,
    POLICY_VERSION,
    Reservation,
    ReservationRequest,
    ReservationState,
    SettlementReceipt,
    new_attempt_id,
    new_event_id,
    new_reservation_id,
    period_bounds,
    utc_period_day,
)

logger = logging.getLogger(__name__)


def today_utc() -> str:
    return datetime_now_utc()[:10]


def datetime_now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════
#  Contract (§2.7)
# ═══════════════════════════════════════════════════════════════════════


class BucketLimit:
    """One applicable budget bucket and its currently effective limit,
    resolved by the budget service (policy) before admission."""

    __slots__ = ("scope", "subject_id", "limit")

    def __init__(self, scope: BudgetScope, subject_id: str, limit: int):
        self.scope = scope
        self.subject_id = subject_id
        self.limit = int(limit)

    def as_dict(self) -> Dict[str, Any]:
        return {"scope": self.scope.value, "subject_id": self.subject_id, "limit": self.limit}


@runtime_checkable
class UsageRepository(Protocol):
    """Durable reservation + consumption storage.

    Implementations MUST provide identical business semantics: the contract
    tests run against both SQLite and Firestore. Amounts are never trusted
    from callers for release — the original hold is loaded from the stored
    reservation. All operations are idempotent under replay.
    """

    # ── reservation lifecycle ──
    async def reserve(
        self, request: ReservationRequest, buckets: List[BucketLimit]
    ) -> Reservation: ...
    async def mark_dispatching(
        self, reservation_id: str, expected_version: Optional[int] = None
    ) -> Reservation: ...
    async def settle(self, reservation_id: str, receipt: SettlementReceipt) -> Reservation: ...
    async def release_unused(
        self, reservation_id: str, evidence: Optional[Dict[str, Any]] = None
    ) -> Reservation: ...
    async def mark_uncertain(self, reservation_id: str, reason: str) -> Reservation: ...
    async def get_reservation(self, reservation_id: str) -> Optional[Reservation]: ...

    # ── snapshots / reconciliation / correction ──
    async def get_budget_snapshot(
        self,
        scope: BudgetScope,
        subject_id: str,
        period: str,
        limit: int,
        unit: BudgetUnit = BudgetUnit.TOKENS,
    ) -> BudgetSnapshot: ...
    async def list_reconciliation_candidates(
        self,
        older_than: datetime,
        states: Optional[List[ReservationState]] = None,
        limit: int = 100,
    ) -> List[Reservation]: ...
    async def record_adjustment(
        self,
        scope: BudgetScope,
        subject_id: str,
        period: str,
        units: int,
        reason: str,
        actor: str,
        correction_id: str,
    ) -> None: ...

    # ── legacy daily-ledger surface (pre-Phase-2 compatibility) ──
    async def add_usage(self, user_id: str, day: str, tokens: int) -> None: ...
    async def get_usage(self, user_id: str, day: str) -> int: ...

    # ── §12.14: authoritative per-run usage (both stores) ──
    async def usage_summary_for_run(self, run_id: str) -> Dict[str, Any]: ...


# ═══════════════════════════════════════════════════════════════════════
#  SQLite implementation
# ═══════════════════════════════════════════════════════════════════════


class SQLiteUsageRepository:
    """Contract implementation over SQLite. Every mutation is one
    dedicated-connection BEGIN IMMEDIATE transaction: reservation rows,
    bucket counters, and accounting events commit together or not at all.
    The write lock is held across the check-then-write, so a fresh bucket
    row and an existing one enforce the cap identically (the §2.0
    first-insert defect is structurally impossible here)."""

    DEFAULT_RESERVATION_TTL = timedelta(hours=6)

    def __init__(self, db_manager):
        self.db = db_manager

    # ── transaction helpers ───────────────────────────────────────────

    @asynccontextmanager
    async def _write_tx(self) -> AsyncIterator[Any]:
        async with self.db.write_transaction() as conn:
            yield conn

    @staticmethod
    async def _event(conn, event: BudgetEvent) -> None:
        await conn.execute(
            """
            INSERT INTO budget_events (
                event_id, event_type, reservation_id, scope, subject_id,
                period, unit, units, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.event_type.value,
                event.reservation_id,
                event.scope.value,
                event.subject_id,
                event.period,
                event.payload.get("unit", BudgetUnit.TOKENS.value),
                int(event.payload.get("units", 0)),
                json.dumps(event.payload, default=str),
                event.created_at or datetime_now_utc(),
            ),
        )

    @staticmethod
    def _bucket_id(scope: BudgetScope, subject_id: str, period: str, unit: BudgetUnit) -> str:
        return f"{scope.value}|{subject_id}|{period}|{unit.value}"

    @staticmethod
    async def _load_bucket(conn, scope, subject_id, period, unit=BudgetUnit.TOKENS):
        return await (
            await conn.execute(
                """
                SELECT * FROM budget_buckets
                WHERE scope = ? AND subject_id = ? AND period = ? AND unit = ?
                """,
                (scope.value, subject_id, period, unit.value),
            )
        ).fetchone()

    @staticmethod
    async def _bump_bucket(conn, scope, subject_id, period, unit, *, consumed=0, reserved=0, uncertain=0, limit_applied=None, policy_version=None):
        """Upsert a bucket delta. The INSERT branch and the UPDATE branch
        apply the same invariants, so a first write can never bypass a cap
        checked by the caller inside the same write transaction."""
        await conn.execute(
            """
            INSERT INTO budget_buckets (
                scope, subject_id, period, unit, consumed, reserved,
                uncertain_reserved, legacy_seed, limit_applied,
                policy_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(scope, subject_id, period, unit) DO UPDATE SET
                consumed = consumed + excluded.consumed,
                reserved = reserved + excluded.reserved,
                uncertain_reserved = uncertain_reserved + excluded.uncertain_reserved,
                limit_applied = COALESCE(excluded.limit_applied, budget_buckets.limit_applied),
                policy_version = COALESCE(excluded.policy_version, budget_buckets.policy_version),
                updated_at = excluded.updated_at
            """,
            (
                scope.value, subject_id, period, unit.value,
                int(consumed), int(reserved), int(uncertain),
                limit_applied, policy_version, datetime_now_utc(),
            ),
        )

    @classmethod
    async def _legacy_delta(cls, conn, subject_id: str, period: str) -> int:
        """Settled consumption present in the pre-Phase-2 usage_ledger that
        the bucket does not mirror yet (old workers, migrated history)."""
        row = await (
            await conn.execute(
                "SELECT tokens, reserved FROM usage_ledger WHERE user_id = ? AND day = ?",
                (subject_id, period),
            )
        ).fetchone()
        if not row:
            return 0
        bucket = await cls._load_bucket(conn, BudgetScope.USER, subject_id, period)
        seed = int(bucket["legacy_seed"]) if bucket else 0
        return max(0, int(row["tokens"] or 0) - seed)

    # ── legacy surface (compatibility) ────────────────────────────────

    async def add_usage(self, user_id: str, day: str, tokens: int) -> None:
        """Record settled spend. Mirrors into the Phase 2 user bucket and
        advances `legacy_seed` so the same tokens can never be counted once
        by the ledger and once by the bucket (§2.18 no dual-write double
        counting)."""
        if tokens <= 0:
            return
        now = datetime_now_utc()
        async with self._write_tx() as conn:
            await conn.execute(
                """
                INSERT INTO usage_ledger (user_id, day, tokens, reserved, updated_at)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(user_id, day) DO UPDATE SET
                    tokens = tokens + excluded.tokens,
                    updated_at = excluded.updated_at
                """,
                (user_id, day, int(tokens), now),
            )
            bucket = await self._load_bucket(conn, BudgetScope.USER, user_id, day)
            if bucket is None:
                ledger = await (
                    await conn.execute(
                        "SELECT tokens FROM usage_ledger WHERE user_id = ? AND day = ?",
                        (user_id, day),
                    )
                ).fetchone()
                seed = int(ledger["tokens"]) if ledger else int(tokens)
                await conn.execute(
                    """
                    INSERT INTO budget_buckets (
                        scope, subject_id, period, unit, consumed, reserved,
                        uncertain_reserved, legacy_seed, policy_version, updated_at
                    ) VALUES (?, ?, ?, 'tokens', ?, 0, 0, ?, ?, ?)
                    ON CONFLICT(scope, subject_id, period, unit) DO UPDATE SET
                        consumed = consumed + excluded.consumed,
                        legacy_seed = excluded.legacy_seed,
                        updated_at = excluded.updated_at
                    """,
                    (BudgetScope.USER.value, user_id, day, int(tokens), seed, POLICY_VERSION, now),
                )
                await self._event(conn, BudgetEvent(
                    event_id=new_event_id(), event_type=BudgetEventType.LEGACY_SEED,
                    reservation_id=None, scope=BudgetScope.USER, subject_id=user_id,
                    period=day, payload={"units": seed, "source": "usage_ledger_seed"},
                    created_at=now,
                ))
            else:
                await self._bump_bucket(conn, BudgetScope.USER, user_id, day, BudgetUnit.TOKENS,
                                        consumed=int(tokens))
                await conn.execute(
                    "UPDATE budget_buckets SET legacy_seed = legacy_seed + ? "
                    "WHERE scope = ? AND subject_id = ? AND period = ? AND unit = 'tokens'",
                    (int(tokens), BudgetScope.USER.value, user_id, day),
                )

    async def get_usage(self, user_id: str, day: str) -> int:
        row = await self.db.fetch_one(
            "SELECT tokens FROM usage_ledger WHERE user_id = ? AND day = ?",
            (user_id, day),
        )
        return int(row["tokens"]) if row else 0

    async def usage_summary_for_run(self, run_id: str) -> Dict[str, Any]:
        """§12.14: authoritative per-run usage from the durable ledger —
        measured vs estimated vs reserved vs uncertain, attributed by the
        ACTUAL model on each reservation. Restarts and replicas read the
        same rows; nothing is drained or consumed."""
        rows = await self.db.fetch_all(
            "SELECT provider, model, state, units, settled_units, "
            "measurement_source, purpose, pricing_version "
            "FROM budget_reservations WHERE run_id=?", (run_id,))
        summary: Dict[str, Any] = {
            "run_id": run_id,
            "measured_units": 0,       # settled, provider_reported
            "estimated_units": 0,      # settled, estimated
            "reserved_units": 0,       # active holds (reserved/dispatching)
            "uncertain_units": 0,      # may have incurred; still counted
            "released_units": 0,       # proven unused
            "total_units": 0,          # charged-or-likely: measured+estimated+uncertain
            "models_used": [],         # actual model history (ordered, deduped)
            "providers": [],
            "by_purpose": {},
            "pricing_version": None,
            "measurement_source": None,
            "reservation_count": 0,
        }
        seen_models, seen_providers = [], []
        sources = set()
        for r in rows or []:
            units = int(r["settled_units"]) if r["settled_units"] is not None \
                else int(r["units"] or 0)
            state = r["state"]
            source = r["measurement_source"]
            sources.add(source)
            summary["reservation_count"] += 1
            if state in ("settled",) and source == "provider_reported":
                summary["measured_units"] += units
            elif state in ("settled",):
                summary["estimated_units"] += units
            elif state in ("reserved", "dispatching"):
                summary["reserved_units"] += units
            elif state == "uncertain":
                summary["uncertain_units"] += units
            elif state == "released":
                summary["released_units"] += units
            summary["total_units"] = (summary["measured_units"]
                                      + summary["estimated_units"]
                                      + summary["uncertain_units"])
            model = r["model"]
            if model and model not in seen_models:
                seen_models.append(model)
            if r["provider"] and r["provider"] not in seen_providers:
                seen_providers.append(r["provider"])
            purpose = r["purpose"] or "generation"
            summary["by_purpose"][purpose] = \
                summary["by_purpose"].get(purpose, 0) + units
            if r["pricing_version"] and not summary["pricing_version"]:
                summary["pricing_version"] = r["pricing_version"]
        summary["models_used"] = seen_models
        summary["providers"] = seen_providers
        summary["measurement_source"] = (
            sorted(sources)[0] if sources else None)
        return summary

    async def reserve_usage(self, user_id: str, day: str, tokens: int, cap: int) -> bool:
        """DEPRECATED legacy shim (pre-Phase-2 signature). Retained for old
        tests/workers; paid execution goes through `reserve()`. The cap check
        runs inside the write transaction over the full committed figure, so
        a first insert cannot bypass it (§2.0 defect 1, fixed on this path
        too)."""
        if tokens <= 0:
            return True
        now = datetime_now_utc()
        async with self._write_tx() as conn:
            committed = await self._committed_in_tx(conn, user_id, day)
            if committed + int(tokens) > int(cap):
                return False
            await conn.execute(
                """
                INSERT INTO usage_ledger (user_id, day, tokens, reserved, updated_at)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(user_id, day) DO UPDATE SET
                    reserved = reserved + excluded.reserved,
                    updated_at = excluded.updated_at
                """,
                (user_id, day, int(tokens), now),
            )
            await self._bump_bucket(conn, BudgetScope.USER, user_id, day, BudgetUnit.TOKENS,
                                    reserved=int(tokens), limit_applied=int(cap))
            await self._event(conn, BudgetEvent(
                event_id=new_event_id(), event_type=BudgetEventType.RESERVATION_CREATED,
                reservation_id=None, scope=BudgetScope.USER, subject_id=user_id,
                period=day, payload={"units": int(tokens), "legacy": True}, created_at=now,
            ))
        return True

    async def settle_usage(self, user_id: str, day: str, reserved_tokens: int, actual_tokens: int) -> None:
        """DEPRECATED legacy shim without reservation identity (kept only for
        old callers — replay here is NOT deduplicated; that is precisely why
        paid execution must use `settle()`)."""
        release = int(max(0, reserved_tokens))
        spend = int(max(0, actual_tokens))
        now = datetime_now_utc()
        async with self._write_tx() as conn:
            row = await (
                await conn.execute(
                    "SELECT tokens, reserved FROM usage_ledger WHERE user_id = ? AND day = ?",
                    (user_id, day),
                )
            ).fetchone()
            if row is not None:
                await conn.execute(
                    """
                    UPDATE usage_ledger
                    SET reserved = MAX(0, reserved - ?), tokens = tokens + ?, updated_at = ?
                    WHERE user_id = ? AND day = ?
                    """,
                    (release, spend, now, user_id, day),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO usage_ledger (user_id, day, tokens, reserved, updated_at)
                    VALUES (?, ?, ?, 0, ?)
                    """,
                    (user_id, day, spend, now),
                )
            await self._bump_bucket(conn, BudgetScope.USER, user_id, day, BudgetUnit.TOKENS,
                                    consumed=spend, reserved=-release)
            await conn.execute(
                "UPDATE budget_buckets SET legacy_seed = legacy_seed + ? "
                "WHERE scope = ? AND subject_id = ? AND period = ? AND unit = 'tokens'",
                (spend, BudgetScope.USER.value, user_id, day),
            )

    async def _committed_in_tx(self, conn, user_id: str, period: str) -> int:
        """Settled + held + un-mirrored legacy spend, read inside the write
        transaction. This is the number every cap check counts."""
        bucket = await self._load_bucket(conn, BudgetScope.USER, user_id, period)
        if bucket is None:
            row = await (
                await conn.execute(
                    "SELECT tokens, reserved FROM usage_ledger WHERE user_id = ? AND day = ?",
                    (user_id, period),
                )
            ).fetchone()
            if not row:
                return 0
            return int(row["tokens"] or 0) + int(row["reserved"] or 0)
        legacy = await self._legacy_delta(conn, user_id, period)
        return (
            int(bucket["consumed"] or 0)
            + int(bucket["reserved"] or 0)
            + legacy
        )

    async def get_committed_usage(self, user_id: str, day: str) -> int:
        async with self._write_tx() as conn:
            return await self._committed_in_tx(conn, user_id, day)

    # ── Phase 2 contract ──────────────────────────────────────────────

    async def reserve(
        self, request: ReservationRequest, buckets: List[BucketLimit]
    ) -> Reservation:
        if request.units <= 0:
            raise BudgetError(
                BudgetErrorCode.BUDGET_POLICY_INVALID,
                "reservation units must be positive",
            )
        if not buckets:
            raise BudgetError(
                BudgetErrorCode.BUDGET_POLICY_INVALID,
                "no budget buckets resolved for this caller — refusing to admit paid work",
            )
        reservation_id = new_reservation_id()  # generated OUTSIDE the transaction
        request_key = request.effective_request_key()
        digest = request.fingerprint()
        period = request.period or utc_period_day()
        now = datetime_now_utc()
        expires_at = (datetime.now(timezone.utc) + self.DEFAULT_RESERVATION_TTL).isoformat()
        bucket_dicts = [b.as_dict() for b in buckets]

        async with self._write_tx() as conn:
            # 1. Idempotent replay by unique request key.
            existing = await (
                await conn.execute(
                    "SELECT * FROM budget_reservations WHERE request_key = ?",
                    (request_key,),
                )
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != digest:
                    raise BudgetError(
                        BudgetErrorCode.RESERVATION_CONFLICT,
                        "request key reused with a different payload",
                        details={"reservation_id": existing["reservation_id"]},
                    )
                return Reservation.from_dict(dict(existing))

            # 2. Validate EVERY applicable allowance inside the write lock —
            #    fresh buckets and existing buckets are checked identically.
            for b in buckets:
                if b.scope == BudgetScope.USER:
                    committed = await self._committed_in_tx(conn, b.subject_id, period)
                else:
                    row = await self._load_bucket(conn, b.scope, b.subject_id, period)
                    committed = (
                        int(row["consumed"] or 0) + int(row["reserved"] or 0)
                        if row else 0
                    )
                if committed + request.units > b.limit:
                    raise BudgetError(
                        BudgetErrorCode.BUDGET_EXHAUSTED,
                        f"budget exhausted for scope={b.scope.value}",
                        available=max(0, b.limit - committed),
                        required=request.units,
                        unit=request.unit,
                    )

            # 3. Insert the reservation, raise the holds, append the event.
            await conn.execute(
                """
                INSERT INTO budget_reservations (
                    reservation_id, request_key, request_digest, user_id,
                    organization_id, run_id, task_id, call_id, attempt_id,
                    provider, model, credential_owner_reference, purpose,
                    unit, units, state, period, policy_version, buckets_json,
                    created_at, updated_at, expires_at, measurement_source,
                    record_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    reservation_id, request_key, digest, request.user_id,
                    request.organization_id, request.run_id, request.task_id,
                    request.call_id, request.attempt_id, request.provider,
                    request.model, request.credential_owner_reference,
                    request.purpose, request.unit.value, int(request.units),
                    ReservationState.RESERVED.value, period, POLICY_VERSION,
                    json.dumps(bucket_dicts), now, now, expires_at,
                    MeasurementSource.ESTIMATED.value,
                ),
            )
            for b in buckets:
                await self._bump_bucket(
                    conn, b.scope, b.subject_id, period, request.unit,
                    reserved=int(request.units), limit_applied=b.limit,
                    policy_version=POLICY_VERSION,
                )
            await self._event(conn, BudgetEvent(
                event_id=new_event_id(), event_type=BudgetEventType.RESERVATION_CREATED,
                reservation_id=reservation_id, scope=BudgetScope.USER,
                subject_id=request.user_id, period=period,
                payload={"units": int(request.units), "buckets": bucket_dicts,
                         "provider": request.provider, "model": request.model},
                created_at=now,
            ))

            stored = await (
                await conn.execute(
                    "SELECT * FROM budget_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                )
            ).fetchone()
            return Reservation.from_dict(dict(stored))

    async def _load_reservation_tx(self, conn, reservation_id: str):
        row = await (
            await conn.execute(
                "SELECT * FROM budget_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )
        ).fetchone()
        if row is None:
            raise BudgetError(
                BudgetErrorCode.RESERVATION_NOT_FOUND,
                f"unknown reservation {reservation_id}",
            )
        return row

    @staticmethod
    def _decode_buckets(row) -> List[Dict[str, str]]:
        try:
            buckets = json.loads(row["buckets_json"] or "[]")
        except (TypeError, ValueError):
            buckets = []
        return buckets or []

    @classmethod
    async def _release_hold(cls, conn, row, *, consumed_delta: int, state: ReservationState,
                            now: str) -> None:
        """Apply the hold release for a settle or release: hold comes from
        the stored reservation row — never from caller-supplied amounts."""
        hold = int(row["units"] or 0)
        was_uncertain = row["state"] == ReservationState.UNCERTAIN.value
        for b in cls._decode_buckets(row):
            await cls._bump_bucket(
                conn, BudgetScope(b["scope"]), b["subject_id"], row["period"],
                BudgetUnit(row["unit"]),
                consumed=int(consumed_delta), reserved=-hold,
                uncertain=-hold if was_uncertain else 0,
            )

    async def mark_dispatching(
        self, reservation_id: str, expected_version: Optional[int] = None
    ) -> Reservation:
        now = datetime_now_utc()
        async with self._write_tx() as conn:
            row = await self._load_reservation_tx(conn, reservation_id)
            if expected_version is not None and int(row["record_version"]) != int(expected_version):
                raise BudgetError(
                    BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                    "dispatch intent lost a version race; re-read before retry",
                )
            if row["state"] != ReservationState.RESERVED.value:
                if row["state"] == ReservationState.DISPATCHING.value:
                    stored = dict(row)
                    stored["record_version"] = int(row["record_version"])
                    return Reservation.from_dict(stored)  # idempotent
                raise BudgetError(
                    BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                    f"cannot dispatch a reservation in state {row['state']}",
                )
            await conn.execute(
                """
                UPDATE budget_reservations
                SET state = ?, dispatch_started_at = ?, updated_at = ?,
                    record_version = record_version + 1
                WHERE reservation_id = ? AND record_version = ?
                """,
                (ReservationState.DISPATCHING.value, now, now, reservation_id,
                 int(row["record_version"])),
            )
            await self._event(conn, BudgetEvent(
                event_id=new_event_id(), event_type=BudgetEventType.DISPATCH_MARKED,
                reservation_id=reservation_id, scope=BudgetScope.USER,
                subject_id=row["user_id"], period=row["period"],
                payload={"units": int(row["units"])}, created_at=now,
            ))
            return await self._reread(conn, reservation_id)

    @classmethod
    async def _reread(cls, conn, reservation_id: str) -> Reservation:
        row = await (
            await conn.execute(
                "SELECT * FROM budget_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )
        ).fetchone()
        return Reservation.from_dict(dict(row))

    async def settle(self, reservation_id: str, receipt: SettlementReceipt) -> Reservation:
        """Settle atomically: decrease holds, increase consumption, store the
        receipt. Replaying the SAME receipt returns the prior result without
        adding consumption (§2.1 G4); a conflicting receipt raises
        SETTLEMENT_CONFLICT and goes through the correction path."""
        if receipt.units < 0:
            raise BudgetError(
                BudgetErrorCode.BUDGET_POLICY_INVALID,
                "settlement units cannot be negative",
            )
        now = datetime_now_utc()
        async with self._write_tx() as conn:
            row = await self._load_reservation_tx(conn, reservation_id)
            if row["state"] == ReservationState.SETTLED.value:
                if row["receipt_digest"] == receipt.digest():
                    return await self._reread(conn, reservation_id)  # idempotent replay
                raise BudgetError(
                    BudgetErrorCode.SETTLEMENT_CONFLICT,
                    "reservation already settled with a different receipt",
                    details={"reservation_id": reservation_id},
                )
            if row["state"] == ReservationState.RELEASED.value:
                raise BudgetError(
                    BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                    "cannot settle a released reservation",
                )
            hold = int(row["units"] or 0)
            actual = int(receipt.units)
            await self._release_hold(conn, row, consumed_delta=actual,
                                     state=ReservationState.SETTLED, now=now)
            await conn.execute(
                """
                UPDATE budget_reservations
                SET state = ?, settled_units = ?, receipt_digest = ?,
                    provider_request_id = ?, measurement_source = ?,
                    pricing_version = ?, updated_at = ?,
                    record_version = record_version + 1
                WHERE reservation_id = ?
                """,
                (ReservationState.SETTLED.value, actual, receipt.digest(),
                 receipt.provider_request_id, receipt.measurement_source.value,
                 receipt.pricing_version, now, reservation_id),
            )
            await self._event(conn, BudgetEvent(
                event_id=new_event_id(), event_type=BudgetEventType.SETTLED,
                reservation_id=reservation_id, scope=BudgetScope.USER,
                subject_id=row["user_id"], period=row["period"],
                payload={
                    "unit": row["unit"], "reserved_units": hold, "actual_units": actual,
                    "overrun": actual > hold,
                    "measurement_source": receipt.measurement_source.value,
                    "provider_request_id": receipt.provider_request_id,
                    # 30.32: provider-specific dimensions and the priced
                    # amount stay inspectable on the settled event.
                    "input_tokens": receipt.input_tokens,
                    "output_tokens": receipt.output_tokens,
                    "cached_input_tokens": receipt.cached_input_tokens,
                    "reasoning_tokens": receipt.reasoning_tokens,
                    "reported_total_tokens": receipt.reported_total_tokens,
                    "resolved_model": receipt.resolved_model,
                    "pricing_version": receipt.pricing_version,
                    "cost_amount": receipt.cost_amount,
                    "cost_currency": receipt.cost_currency,
                },
                created_at=now,
            ))
            return await self._reread(conn, reservation_id)

    async def release_unused(
        self, reservation_id: str, evidence: Optional[Dict[str, Any]] = None
    ) -> Reservation:
        now = datetime_now_utc()
        async with self._write_tx() as conn:
            row = await self._load_reservation_tx(conn, reservation_id)
            if row["state"] == ReservationState.RELEASED.value:
                return await self._reread(conn, reservation_id)  # idempotent
            if row["state"] == ReservationState.SETTLED.value:
                raise BudgetError(
                    BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                    "cannot release a settled reservation; use an adjustment",
                )
            await self._release_hold(conn, row, consumed_delta=0,
                                     state=ReservationState.RELEASED, now=now)
            await conn.execute(
                """
                UPDATE budget_reservations
                SET state = ?, updated_at = ?, record_version = record_version + 1
                WHERE reservation_id = ?
                """,
                (ReservationState.RELEASED.value, now, reservation_id),
            )
            await self._event(conn, BudgetEvent(
                event_id=new_event_id(), event_type=BudgetEventType.RELEASED,
                reservation_id=reservation_id, scope=BudgetScope.USER,
                subject_id=row["user_id"], period=row["period"],
                payload={"units": int(row["units"] or 0), "evidence": evidence or {}},
                created_at=now,
            ))
            return await self._reread(conn, reservation_id)

    async def mark_uncertain(self, reservation_id: str, reason: str) -> Reservation:
        now = datetime_now_utc()
        async with self._write_tx() as conn:
            row = await self._load_reservation_tx(conn, reservation_id)
            if row["state"] == ReservationState.UNCERTAIN.value:
                return await self._reread(conn, reservation_id)  # idempotent
            if row["state"] not in {ReservationState.RESERVED.value, ReservationState.DISPATCHING.value}:
                raise BudgetError(
                    BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                    f"cannot mark a reservation in state {row['state']} uncertain",
                )
            hold = int(row["units"] or 0)
            for b in self._decode_buckets(row):
                await self._bump_bucket(
                    conn, BudgetScope(b["scope"]), b["subject_id"], row["period"],
                    BudgetUnit(row["unit"]), uncertain=hold,
                )
            await conn.execute(
                """
                UPDATE budget_reservations
                SET state = ?, updated_at = ?, record_version = record_version + 1
                WHERE reservation_id = ?
                """,
                (ReservationState.UNCERTAIN.value, now, reservation_id),
            )
            await self._event(conn, BudgetEvent(
                event_id=new_event_id(), event_type=BudgetEventType.MARKED_UNCERTAIN,
                reservation_id=reservation_id, scope=BudgetScope.USER,
                subject_id=row["user_id"], period=row["period"],
                payload={"units": hold, "reason": reason}, created_at=now,
            ))
            return await self._reread(conn, reservation_id)

    async def get_reservation(self, reservation_id: str) -> Optional[Reservation]:
        row = await self.db.fetch_one(
            "SELECT * FROM budget_reservations WHERE reservation_id = ?",
            (reservation_id,),
        )
        return Reservation.from_dict(dict(row)) if row else None

    async def get_budget_snapshot(
        self,
        scope: BudgetScope,
        subject_id: str,
        period: str,
        limit: int,
        unit: BudgetUnit = BudgetUnit.TOKENS,
    ) -> BudgetSnapshot:
        bucket = await self.db.fetch_one(
            """
            SELECT * FROM budget_buckets
            WHERE scope = ? AND subject_id = ? AND period = ? AND unit = ?
            """,
            (scope.value, subject_id, period, unit.value),
        )
        consumed = int(bucket["consumed"] or 0) if bucket else 0
        reserved = int(bucket["reserved"] or 0) if bucket else 0
        uncertain = int(bucket["uncertain_reserved"] or 0) if bucket else 0
        if scope == BudgetScope.USER:
            consumed += await self._legacy_delta_outside_tx(subject_id, period)
        start, end = period_bounds(period)
        return BudgetSnapshot(
            scope=scope, subject_id=subject_id, unit=unit, period=period,
            period_start=start, period_end=end, limit=int(limit),
            consumed=consumed, reserved=reserved, uncertain_reserved=uncertain,
            accounting_status="healthy", policy_version=POLICY_VERSION,
        )

    async def _legacy_delta_outside_tx(self, subject_id: str, period: str) -> int:
        async with self._write_tx() as conn:
            return await self._legacy_delta(conn, subject_id, period)

    async def list_reconciliation_candidates(
        self,
        older_than: datetime,
        states: Optional[List[ReservationState]] = None,
        limit: int = 100,
    ) -> List[Reservation]:
        states = states or [ReservationState.DISPATCHING, ReservationState.UNCERTAIN]
        placeholders = ",".join("?" for _ in states)
        rows = await self.db.fetch_all(
            f"""
            SELECT * FROM budget_reservations
            WHERE state IN ({placeholders}) AND updated_at < ?
            ORDER BY updated_at ASC LIMIT ?
            """,
            tuple(s.value for s in states) + (older_than.isoformat(), int(limit)),
        )
        return [Reservation.from_dict(dict(r)) for r in rows]

    async def record_adjustment(
        self,
        scope: BudgetScope,
        subject_id: str,
        period: str,
        units: int,
        reason: str,
        actor: str,
        correction_id: str,
    ) -> None:
        """Authorized correction: append-only event with idempotent
        correction_id; totals move only through this audited path (§2.19)."""
        now = datetime_now_utc()
        async with self._write_tx() as conn:
            existing = await (
                await conn.execute(
                    "SELECT 1 FROM budget_events WHERE event_id = ?", (correction_id,)
                )
            ).fetchone()
            if existing:
                return  # correction already applied
            await self._bump_bucket(conn, scope, subject_id, period,
                                    BudgetUnit.TOKENS, consumed=int(units))
            await self._event(conn, BudgetEvent(
                event_id=correction_id, event_type=BudgetEventType.ADJUSTMENT,
                reservation_id=None, scope=scope, subject_id=subject_id,
                period=period, payload={"units": int(units), "reason": reason,
                                        "actor": actor},
                created_at=now,
            ))


# ═══════════════════════════════════════════════════════════════════════
#  Firestore implementation (§2.9 — same contract, atomic transactions)
# ═══════════════════════════════════════════════════════════════════════


class FirestoreUsageRepository:
    """Firestore parity for the reservation contract.

    Firestore transaction callbacks may execute more than once under
    contention and reads must precede writes, so identifiers are generated
    BEFORE the callback and no external effects happen inside it. Counter
    updates are read-modify-write inside the transaction — atomic admission
    control, not client-side read + Increment.
    """

    DEFAULT_RESERVATION_TTL_HOURS = 6

    _COLL_RESERVATIONS = "budget_reservations"
    _COLL_BY_KEY = "budget_reservations_by_key"
    _COLL_BUCKETS = "budget_buckets"
    _COLL_EVENTS = "budget_events"
    _COLL_LEGACY = "usage_ledger"

    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    @staticmethod
    def _bucket_doc_id(scope, subject_id, period, unit) -> str:
        return f"{scope.value}|{subject_id}|{period}|{unit.value}"

    @staticmethod
    def _legacy_doc_id(user_id: str, day: str) -> str:
        return f"{user_id}_{day}"

    @staticmethod
    def _res_from_doc(data: Dict[str, Any]) -> Reservation:
        return Reservation.from_dict(data)

    # ── legacy surface ────────────────────────────────────────────────

    async def add_usage(self, user_id: str, day: str, tokens: int) -> None:
        if tokens <= 0:
            return
        from google.cloud import firestore as gcf
        db = await self._get_db()
        ledger_ref = db.collection(self._COLL_LEGACY).document(self._legacy_doc_id(user_id, day))
        bucket_ref = db.collection(self._COLL_BUCKETS).document(
            self._bucket_doc_id(BudgetScope.USER, user_id, day, BudgetUnit.TOKENS)
        )
        now = datetime_now_utc()

        @gcf.async_transactional
        async def mirror(transaction):
            ledger_snap = await ledger_ref.get(transaction=transaction)
            ledger_tokens = int((ledger_snap.to_dict() or {}).get("tokens") or 0) + int(tokens)
            bucket_snap = await bucket_ref.get(transaction=transaction)
            bdata = bucket_snap.to_dict() or {}
            consumed = int(bdata.get("consumed") or 0) + int(tokens)
            seed = int(bdata.get("legacy_seed") or 0) + int(tokens)
            transaction.set(ledger_ref, {
                "user_id": user_id, "day": day, "tokens": ledger_tokens,
                "reserved": int((ledger_snap.to_dict() or {}).get("reserved") or 0),
                "updated_at": now,
            }, merge=True)
            transaction.set(bucket_ref, {
                "scope": BudgetScope.USER.value, "subject_id": user_id,
                "period": day, "unit": BudgetUnit.TOKENS.value,
                "consumed": consumed, "reserved": int(bdata.get("reserved") or 0),
                "uncertain_reserved": int(bdata.get("uncertain_reserved") or 0),
                "legacy_seed": seed, "policy_version": POLICY_VERSION,
                "updated_at": now,
            }, merge=True)
            transaction.set(db.collection(self._COLL_EVENTS).document(new_event_id()), {
                "event_type": BudgetEventType.LEGACY_SEED.value if not bucket_snap.exists else BudgetEventType.SETTLED.value,
                "scope": BudgetScope.USER.value, "subject_id": user_id,
                "period": day, "unit": BudgetUnit.TOKENS.value, "units": int(tokens),
                "payload": {"mirror_of": "add_usage"}, "created_at": now,
            })

        await mirror(db.transaction())

    async def get_usage(self, user_id: str, day: str) -> int:
        db = await self._get_db()
        snap = await db.collection(self._COLL_LEGACY).document(self._legacy_doc_id(user_id, day)).get()
        if not snap.exists:
            return 0
        return int((snap.to_dict() or {}).get("tokens") or 0)

    async def usage_summary_for_run(self, run_id: str) -> Dict[str, Any]:
        """§12.14 Firestore parity: authoritative per-run usage from the
        durable reservations — same aggregation semantics as SQLite."""
        db = await self._get_db()
        query = (db.collection("budget_reservations")
                 .where("run_id", "==", run_id).limit(500))
        summary: Dict[str, Any] = {
            "run_id": run_id,
            "measured_units": 0, "estimated_units": 0, "reserved_units": 0,
            "uncertain_units": 0, "released_units": 0, "total_units": 0,
            "models_used": [], "providers": [], "by_purpose": {},
            "pricing_version": None, "measurement_source": None,
            "reservation_count": 0,
        }
        seen_models, seen_providers, sources = [], [], set()
        async for snap in query.stream():
            r = snap.to_dict() or {}
            settled = r.get("settled_units")
            units = int(settled) if settled is not None else int(r.get("units") or 0)
            state = r.get("state")
            source = r.get("measurement_source")
            sources.add(source)
            summary["reservation_count"] += 1
            if state == "settled" and source == "provider_reported":
                summary["measured_units"] += units
            elif state == "settled":
                summary["estimated_units"] += units
            elif state in ("reserved", "dispatching"):
                summary["reserved_units"] += units
            elif state == "uncertain":
                summary["uncertain_units"] += units
            elif state == "released":
                summary["released_units"] += units
            summary["total_units"] = (summary["measured_units"]
                                      + summary["estimated_units"]
                                      + summary["uncertain_units"])
            model = r.get("model")
            if model and model not in seen_models:
                seen_models.append(model)
            provider = r.get("provider")
            if provider and provider not in seen_providers:
                seen_providers.append(provider)
            purpose = r.get("purpose") or "generation"
            summary["by_purpose"][purpose] = \
                summary["by_purpose"].get(purpose, 0) + units
            if r.get("pricing_version") and not summary["pricing_version"]:
                summary["pricing_version"] = r["pricing_version"]
        summary["models_used"] = seen_models
        summary["providers"] = seen_providers
        summary["measurement_source"] = sorted(sources)[0] if sources else None
        return summary

    async def _committed_from_bucket_doc(self, bdata: Dict[str, Any], legacy_delta: int) -> int:
        return (
            int(bdata.get("consumed") or 0)
            + int(bdata.get("reserved") or 0)
            + legacy_delta
        )

    async def get_committed_usage(self, user_id: str, day: str) -> int:
        db = await self._get_db()
        bucket_snap = await db.collection(self._COLL_BUCKETS).document(
            self._bucket_doc_id(BudgetScope.USER, user_id, day, BudgetUnit.TOKENS)
        ).get()
        ledger_snap = await db.collection(self._COLL_LEGACY).document(self._legacy_doc_id(user_id, day)).get()
        ldata = ledger_snap.to_dict() or {}
        bdata = bucket_snap.to_dict() or {}
        if not bucket_snap.exists:
            return int(ldata.get("tokens") or 0) + int(ldata.get("reserved") or 0)
        seed = int(bdata.get("legacy_seed") or 0)
        legacy_delta = max(0, int(ldata.get("tokens") or 0) - seed)
        return await self._committed_from_bucket_doc(bdata, legacy_delta)

    # ── contract ──────────────────────────────────────────────────────

    async def reserve(
        self, request: ReservationRequest, buckets: List[BucketLimit]
    ) -> Reservation:
        if request.units <= 0:
            raise BudgetError(
                BudgetErrorCode.BUDGET_POLICY_INVALID,
                "reservation units must be positive",
            )
        if not buckets:
            raise BudgetError(
                BudgetErrorCode.BUDGET_POLICY_INVALID,
                "no budget buckets resolved for this caller — refusing to admit paid work",
            )
        from google.cloud import firestore as gcf
        db = await self._get_db()
        reservation_id = new_reservation_id()
        request_key = request.effective_request_key()
        digest = request.fingerprint()
        period = request.period or utc_period_day()
        now = datetime_now_utc()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=self.DEFAULT_RESERVATION_TTL_HOURS)
        ).isoformat()
        bucket_dicts = [b.as_dict() for b in buckets]
        key_ref = db.collection(self._COLL_BY_KEY).document(request_key)

        @gcf.async_transactional
        async def admit(transaction):
            key_snap = await key_ref.get(transaction=transaction)
            if key_snap.exists:
                existing_id = (key_snap.to_dict() or {}).get("reservation_id")
                res_snap = await db.collection(self._COLL_RESERVATIONS).document(existing_id).get(
                    transaction=transaction
                )
                existing = self._res_from_doc(res_snap.to_dict() or {})
                if existing.request_digest != digest:
                    raise BudgetError(
                        BudgetErrorCode.RESERVATION_CONFLICT,
                        "request key reused with a different payload",
                        details={"reservation_id": existing_id},
                    )
                return existing

            for b in buckets:
                bucket_ref = db.collection(self._COLL_BUCKETS).document(
                    self._bucket_doc_id(b.scope, b.subject_id, period, request.unit)
                )
                snap = await bucket_ref.get(transaction=transaction)
                bdata = snap.to_dict() or {}
                committed = int(bdata.get("consumed") or 0) + int(bdata.get("reserved") or 0)
                if b.scope == BudgetScope.USER:
                    legacy_ref = db.collection(self._COLL_LEGACY).document(
                        self._legacy_doc_id(b.subject_id, period)
                    )
                    legacy_snap = await legacy_ref.get(transaction=transaction)
                    ldata = legacy_snap.to_dict() or {}
                    legacy_delta = max(0, int(ldata.get("tokens") or 0) - int(bdata.get("legacy_seed") or 0))
                    committed += legacy_delta
                if committed + request.units > b.limit:
                    raise BudgetError(
                        BudgetErrorCode.BUDGET_EXHAUSTED,
                        f"budget exhausted for scope={b.scope.value}",
                        available=max(0, b.limit - committed),
                        required=request.units,
                        unit=request.unit,
                    )
                transaction.set(bucket_ref, {
                    "scope": b.scope.value, "subject_id": b.subject_id,
                    "period": period, "unit": request.unit.value,
                    "consumed": committed if not snap.exists else int(bdata.get("consumed") or 0),
                    "reserved": int(bdata.get("reserved") or 0) + int(request.units),
                    "uncertain_reserved": int(bdata.get("uncertain_reserved") or 0),
                    "legacy_seed": int(bdata.get("legacy_seed") or 0),
                    "limit_applied": b.limit, "policy_version": POLICY_VERSION,
                    "updated_at": now,
                }, merge=True)

            reservation = Reservation(
                reservation_id=reservation_id, request_key=request_key,
                request_digest=digest, user_id=request.user_id,
                organization_id=request.organization_id, run_id=request.run_id,
                task_id=request.task_id, call_id=request.call_id,
                attempt_id=request.attempt_id, provider=request.provider,
                model=request.model,
                credential_owner_reference=request.credential_owner_reference,
                policy_version=POLICY_VERSION, buckets=bucket_dicts,
                units=int(request.units), state=ReservationState.RESERVED,
                unit=request.unit, period=period, created_at=now, updated_at=now,
                expires_at=expires_at,
                measurement_source=MeasurementSource.ESTIMATED.value,
            )
            transaction.set(
                db.collection(self._COLL_RESERVATIONS).document(reservation_id),
                {**reservation.__dict__, "state": reservation.state.value,
                 "unit": reservation.unit.value, "buckets": bucket_dicts},
            )
            transaction.set(key_ref, {"reservation_id": reservation_id, "created_at": now})
            transaction.set(db.collection(self._COLL_EVENTS).document(new_event_id()), {
                "event_type": BudgetEventType.RESERVATION_CREATED.value,
                "reservation_id": reservation_id,
                "scope": BudgetScope.USER.value, "subject_id": request.user_id,
                "period": period, "unit": request.unit.value,
                "units": int(request.units), "payload": {"buckets": bucket_dicts},
                "created_at": now,
            })
            return reservation

        return await admit(db.transaction())

    async def _get_reservation_doc(self, db, reservation_id: str, transaction):
        snap = await db.collection(self._COLL_RESERVATIONS).document(reservation_id).get(
            transaction=transaction
        )
        if not snap.exists:
            raise BudgetError(
                BudgetErrorCode.RESERVATION_NOT_FOUND,
                f"unknown reservation {reservation_id}",
            )
        return snap.to_dict() or {}

    async def _apply_hold_change(self, transaction, db, res: Dict[str, Any], *,
                                 consumed_delta: int, now: str) -> None:
        hold = int(res.get("units") or 0)
        was_uncertain = res.get("state") == ReservationState.UNCERTAIN.value
        for b in res.get("buckets") or []:
            bucket_ref = db.collection(self._COLL_BUCKETS).document(
                self._bucket_doc_id(BudgetScope(b["scope"]), b["subject_id"],
                                    res.get("period"), BudgetUnit(res.get("unit") or "tokens"))
            )
            snap = await bucket_ref.get(transaction=transaction)
            bdata = snap.to_dict() or {}
            transaction.set(bucket_ref, {
                "scope": b["scope"], "subject_id": b["subject_id"],
                "period": res.get("period"), "unit": res.get("unit") or "tokens",
                "consumed": int(bdata.get("consumed") or 0) + int(consumed_delta),
                "reserved": max(0, int(bdata.get("reserved") or 0) - hold),
                "uncertain_reserved": max(
                    0, int(bdata.get("uncertain_reserved") or 0) - (hold if was_uncertain else 0)
                ),
                "legacy_seed": int(bdata.get("legacy_seed") or 0),
                "updated_at": now,
            }, merge=True)

    async def settle(self, reservation_id: str, receipt: SettlementReceipt) -> Reservation:
        from google.cloud import firestore as gcf
        db = await self._get_db()
        now = datetime_now_utc()
        res_ref = db.collection(self._COLL_RESERVATIONS).document(reservation_id)

        @gcf.async_transactional
        async def apply(transaction):
            res = await self._get_reservation_doc(db, reservation_id, transaction)
            state = res.get("state")
            if state == ReservationState.SETTLED.value:
                if res.get("receipt_digest") == receipt.digest():
                    return self._res_from_doc(res)  # idempotent replay
                raise BudgetError(
                    BudgetErrorCode.SETTLEMENT_CONFLICT,
                    "reservation already settled with a different receipt",
                    details={"reservation_id": reservation_id},
                )
            if state == ReservationState.RELEASED.value:
                raise BudgetError(
                    BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                    "cannot settle a released reservation",
                )
            await self._apply_hold_change(transaction, db, res, consumed_delta=int(receipt.units), now=now)
            updated = {
                **res,
                "state": ReservationState.SETTLED.value,
                "settled_units": int(receipt.units),
                "receipt_digest": receipt.digest(),
                "provider_request_id": receipt.provider_request_id,
                "measurement_source": receipt.measurement_source.value,
                "updated_at": now,
                "record_version": int(res.get("record_version") or 0) + 1,
            }
            transaction.set(res_ref, updated)
            transaction.set(db.collection(self._COLL_EVENTS).document(new_event_id()), {
                "event_type": BudgetEventType.SETTLED.value,
                "reservation_id": reservation_id,
                "scope": BudgetScope.USER.value, "subject_id": res.get("user_id"),
                "period": res.get("period"), "unit": res.get("unit") or "tokens",
                "units": int(receipt.units),
                "payload": {"reserved_units": int(res.get("units") or 0),
                            "overrun": int(receipt.units) > int(res.get("units") or 0)},
                "created_at": now,
            })
            return self._res_from_doc(updated)

        return await apply(db.transaction())

    async def release_unused(
        self, reservation_id: str, evidence: Optional[Dict[str, Any]] = None
    ) -> Reservation:
        from google.cloud import firestore as gcf
        db = await self._get_db()
        now = datetime_now_utc()
        res_ref = db.collection(self._COLL_RESERVATIONS).document(reservation_id)

        @gcf.async_transactional
        async def apply(transaction):
            res = await self._get_reservation_doc(db, reservation_id, transaction)
            state = res.get("state")
            if state == ReservationState.RELEASED.value:
                return self._res_from_doc(res)
            if state == ReservationState.SETTLED.value:
                raise BudgetError(
                    BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                    "cannot release a settled reservation; use an adjustment",
                )
            await self._apply_hold_change(transaction, db, res, consumed_delta=0, now=now)
            updated = {
                **res, "state": ReservationState.RELEASED.value,
                "updated_at": now,
                "record_version": int(res.get("record_version") or 0) + 1,
            }
            transaction.set(res_ref, updated)
            transaction.set(db.collection(self._COLL_EVENTS).document(new_event_id()), {
                "event_type": BudgetEventType.RELEASED.value,
                "reservation_id": reservation_id,
                "scope": BudgetScope.USER.value, "subject_id": res.get("user_id"),
                "period": res.get("period"), "unit": res.get("unit") or "tokens",
                "units": int(res.get("units") or 0), "payload": {"evidence": evidence or {}},
                "created_at": now,
            })
            return self._res_from_doc(updated)

        return await apply(db.transaction())

    async def mark_dispatching(
        self, reservation_id: str, expected_version: Optional[int] = None
    ) -> Reservation:
        from google.cloud import firestore as gcf
        db = await self._get_db()
        now = datetime_now_utc()
        res_ref = db.collection(self._COLL_RESERVATIONS).document(reservation_id)

        @gcf.async_transactional
        async def apply(transaction):
            res = await self._get_reservation_doc(db, reservation_id, transaction)
            state = res.get("state")
            if expected_version is not None and int(res.get("record_version") or 0) != int(expected_version):
                raise BudgetError(
                    BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                    "dispatch intent lost a version race; re-read before retry",
                )
            if state == ReservationState.DISPATCHING.value:
                return self._res_from_doc(res)  # idempotent
            if state != ReservationState.RESERVED.value:
                raise BudgetError(
                    BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                    f"cannot dispatch a reservation in state {state}",
                )
            updated = {
                **res, "state": ReservationState.DISPATCHING.value,
                "dispatch_started_at": now, "updated_at": now,
                "record_version": int(res.get("record_version") or 0) + 1,
            }
            transaction.set(res_ref, updated)
            transaction.set(db.collection(self._COLL_EVENTS).document(new_event_id()), {
                "event_type": BudgetEventType.DISPATCH_MARKED.value,
                "reservation_id": reservation_id,
                "scope": BudgetScope.USER.value, "subject_id": res.get("user_id"),
                "period": res.get("period"), "unit": res.get("unit") or "tokens",
                "units": int(res.get("units") or 0), "payload": {}, "created_at": now,
            })
            return self._res_from_doc(updated)

        return await apply(db.transaction())

    async def mark_uncertain(self, reservation_id: str, reason: str) -> Reservation:
        from google.cloud import firestore as gcf
        db = await self._get_db()
        now = datetime_now_utc()
        res_ref = db.collection(self._COLL_RESERVATIONS).document(reservation_id)

        @gcf.async_transactional
        async def apply(transaction):
            res = await self._get_reservation_doc(db, reservation_id, transaction)
            state = res.get("state")
            if state == ReservationState.UNCERTAIN.value:
                return self._res_from_doc(res)
            if state not in {ReservationState.RESERVED.value, ReservationState.DISPATCHING.value}:
                raise BudgetError(
                    BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                    f"cannot mark a reservation in state {state} uncertain",
                )
            hold = int(res.get("units") or 0)
            for b in res.get("buckets") or []:
                bucket_ref = db.collection(self._COLL_BUCKETS).document(
                    self._bucket_doc_id(BudgetScope(b["scope"]), b["subject_id"],
                                        res.get("period"), BudgetUnit(res.get("unit") or "tokens"))
                )
                snap = await bucket_ref.get(transaction=transaction)
                bdata = snap.to_dict() or {}
                transaction.set(bucket_ref, {
                    **bdata, "scope": b["scope"], "subject_id": b["subject_id"],
                    "period": res.get("period"), "unit": res.get("unit") or "tokens",
                    "uncertain_reserved": int(bdata.get("uncertain_reserved") or 0) + hold,
                    "updated_at": now,
                }, merge=True)
            updated = {
                **res, "state": ReservationState.UNCERTAIN.value,
                "updated_at": now,
                "record_version": int(res.get("record_version") or 0) + 1,
            }
            transaction.set(res_ref, updated)
            transaction.set(db.collection(self._COLL_EVENTS).document(new_event_id()), {
                "event_type": BudgetEventType.MARKED_UNCERTAIN.value,
                "reservation_id": reservation_id,
                "scope": BudgetScope.USER.value, "subject_id": res.get("user_id"),
                "period": res.get("period"), "unit": res.get("unit") or "tokens",
                "units": hold, "payload": {"reason": reason}, "created_at": now,
            })
            return self._res_from_doc(updated)

        return await apply(db.transaction())

    async def get_reservation(self, reservation_id: str) -> Optional[Reservation]:
        db = await self._get_db()
        snap = await db.collection(self._COLL_RESERVATIONS).document(reservation_id).get()
        if not snap.exists:
            return None
        return self._res_from_doc(snap.to_dict() or {})

    async def get_budget_snapshot(
        self,
        scope: BudgetScope,
        subject_id: str,
        period: str,
        limit: int,
        unit: BudgetUnit = BudgetUnit.TOKENS,
    ) -> BudgetSnapshot:
        db = await self._get_db()
        bucket_snap = await db.collection(self._COLL_BUCKETS).document(
            self._bucket_doc_id(scope, subject_id, period, unit)
        ).get()
        bdata = bucket_snap.to_dict() or {}
        consumed = int(bdata.get("consumed") or 0)
        reserved = int(bdata.get("reserved") or 0)
        uncertain = int(bdata.get("uncertain_reserved") or 0)
        if scope == BudgetScope.USER:
            ledger_snap = await db.collection(self._COLL_LEGACY).document(
                self._legacy_doc_id(subject_id, period)
            ).get()
            ldata = ledger_snap.to_dict() or {}
            consumed += max(
                0, int(ldata.get("tokens") or 0) - int(bdata.get("legacy_seed") or 0)
            )
        start, end = period_bounds(period)
        return BudgetSnapshot(
            scope=scope, subject_id=subject_id, unit=unit, period=period,
            period_start=start, period_end=end, limit=int(limit),
            consumed=consumed, reserved=reserved, uncertain_reserved=uncertain,
            accounting_status="healthy", policy_version=POLICY_VERSION,
        )

    async def list_reconciliation_candidates(
        self,
        older_than: datetime,
        states: Optional[List[ReservationState]] = None,
        limit: int = 100,
    ) -> List[Reservation]:
        states = states or [ReservationState.DISPATCHING, ReservationState.UNCERTAIN]
        db = await self._get_db()
        query = (
            db.collection(self._COLL_RESERVATIONS)
            .where("state", "in", [s.value for s in states[:10]])
            .where("updated_at", "<", older_than.isoformat())
            .order_by("updated_at")
            .limit(int(limit))
        )
        out: List[Reservation] = []
        async for doc in query.stream():
            out.append(self._res_from_doc(doc.to_dict() or {}))
        return out

    async def record_adjustment(
        self,
        scope: BudgetScope,
        subject_id: str,
        period: str,
        units: int,
        reason: str,
        actor: str,
        correction_id: str,
    ) -> None:
        from google.cloud import firestore as gcf
        db = await self._get_db()
        now = datetime_now_utc()
        event_ref = db.collection(self._COLL_EVENTS).document(correction_id)
        bucket_ref = db.collection(self._COLL_BUCKETS).document(
            self._bucket_doc_id(scope, subject_id, period, BudgetUnit.TOKENS)
        )

        @gcf.async_transactional
        async def apply(transaction):
            existing = await event_ref.get(transaction=transaction)
            if existing.exists:
                return  # correction already applied
            snap = await bucket_ref.get(transaction=transaction)
            bdata = snap.to_dict() or {}
            transaction.set(bucket_ref, {
                "scope": scope.value, "subject_id": subject_id, "period": period,
                "unit": BudgetUnit.TOKENS.value,
                "consumed": int(bdata.get("consumed") or 0) + int(units),
                "reserved": int(bdata.get("reserved") or 0),
                "uncertain_reserved": int(bdata.get("uncertain_reserved") or 0),
                "legacy_seed": int(bdata.get("legacy_seed") or 0),
                "updated_at": now,
            }, merge=True)
            transaction.set(event_ref, {
                "event_type": BudgetEventType.ADJUSTMENT.value,
                "scope": scope.value, "subject_id": subject_id, "period": period,
                "unit": BudgetUnit.TOKENS.value, "units": int(units),
                "payload": {"reason": reason, "actor": actor}, "created_at": now,
            })

        await apply(db.transaction())


# ── Process-wide holder (wired by RepositoryFactory.initialize) ────────

_repo: Optional[UsageRepository] = None


def set_usage_repo(repo: Optional[UsageRepository]) -> None:
    global _repo
    _repo = repo


def get_usage_repo() -> Optional[UsageRepository]:
    return _repo


async def record_usage_durably(user_id: str, tokens: int) -> None:
    """Persist settled token spend for UNMETERED legacy callers. Fire-and-
    forget friendly: logs and drops on failure rather than breaking the call
    that earned the spend. Metered paths settle through reservations instead
    (exactly one authoritative write per call, §2.18)."""
    repo = get_usage_repo()
    if repo is None or tokens <= 0:
        return
    try:
        await repo.add_usage(user_id, today_utc(), tokens)
    except Exception:
        logger.exception("Durable usage recording failed for %s", user_id)


async def get_durable_usage(user_id: str) -> int:
    repo = get_usage_repo()
    if repo is None:
        return 0
    try:
        return await repo.get_usage(user_id, today_utc())
    except Exception:
        logger.exception("Durable usage read failed for %s", user_id)
        raise


async def get_committed_usage(user_id: str) -> int:
    """Module-level helper: settled actuals + open reservation holds for the
    user's current UTC day — the number the budget cap counts."""
    repo = get_usage_repo()
    if repo is None:
        return 0
    try:
        return await repo.get_committed_usage(user_id, today_utc())
    except Exception:
        logger.exception("Committed usage read failed for %s", user_id)
        raise
