"""Shared durable rate limiting (v5.6 §1; Phase 3 §3.4/§3.5/§3.8).

The in-process sliding window is fast but per-replica: N replicas get N
allowances. This store provides a SHARED fixed-window counter in the durable
database so admission-sensitive groups enforce ONE limit across every replica
and across restarts.

Window representation (§3.4) — the one representation, everywhere:

    window_start = floor(now_epoch / window_seconds) * window_seconds   (EPOCH)
    window_end   = window_start + window_seconds                        (EPOCH)
    cleanup_after = window_end + window_seconds                         (EPOCH)

Rows are purged only when cleanup_after < cutoff, so a currently-effective
counter can never be deleted, whatever cutoff the worker passes. The key
includes window_seconds so configurations with different intervals (or a
changed interval) never collide on one counter. The pre-Phase-3 bug stored a
window ORDINAL (epoch // 60 ≈ 29 million) in a column the worker compared
against epoch seconds (≈ 1.79 billion) — cleanup classified every active
window as expired and reset exhausted allowances mid-window.

High-volume groups stay in-process by design: a database round-trip per
request would trade the right availability/cost balance for a limit that
Cloud Armor enforces better at the edge.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from backend.models.admission import (
    AdmissionDecision,
    AdmissionPolicyError,
    AdmissionReason,
    EnforcementMode,
    StoreFailureMode,
)

logger = logging.getLogger(__name__)

# A window stays purgeable only one full window after its end (§3.4 #5):
# cleanup_after = window_end + window_seconds. Clock skew inside one window
# cannot delete a live counter.
RETENTION_WINDOWS = 1

MAX_IDENTIFIER_LENGTH = 200


def window_bounds(window_seconds: int, now: Optional[float] = None) -> tuple[int, int]:
    """(start_epoch, end_epoch) of the fixed window containing `now`."""
    if int(window_seconds) <= 0:
        raise ValueError(f"window_seconds must be > 0 (got {window_seconds})")
    now_epoch = int(now if now is not None else time.time())
    start = now_epoch - (now_epoch % int(window_seconds))
    return start, start + int(window_seconds)


def current_window(window_seconds: int) -> int:
    """Compat shim: the window's START EPOCH (pre-Phase-3 this returned a
    dimensionless ordinal — the unit mismatch behind the cleanup defect)."""
    return window_bounds(window_seconds)[0]


def validate_identity(identifier: str) -> str:
    """§3.7/§3.5: an empty or malformed identity is an explicit refusal, never
    an accidentally-unlimited bucket. Callers construct identities from
    verified user IDs or the ASGI peer — never raw forwarded headers."""
    if not isinstance(identifier, str):
        raise AdmissionPolicyError("Admission identity must be a string")
    cleaned = identifier.strip()
    if not cleaned:
        raise AdmissionPolicyError("Admission identity must not be empty")
    if len(cleaned) > MAX_IDENTIFIER_LENGTH:
        raise AdmissionPolicyError(
            f"Admission identity exceeds {MAX_IDENTIFIER_LENGTH} characters")
    if any(ch in cleaned for ch in ("\x00", "\n", "\r")):
        raise AdmissionPolicyError("Admission identity contains control characters")
    return cleaned


# ── Typed repository outcome (§3.8) ───────────────────────────────────

@dataclass(frozen=True)
class SharedAdmitResult:
    allowed: bool
    count: int          # counter value AFTER admission (honest `remaining`)
    window_start: int   # epoch
    window_end: int     # epoch


class SharedRateRepository(Protocol):
    async def admit(self, identifier: str, group: str, window_start: int,
                    max_requests: int, window_seconds: int = 60,
                    policy_version: str = "p3.v1") -> SharedAdmitResult: ...

    async def purge_expired(self, older_than_epoch: int) -> int: ...


def _validate_admission_inputs(max_requests: int, window_seconds: int) -> None:
    """§3.5 policy validation — identical semantics in BOTH backends so the
    first record can never bypass the rule that governs existing ones."""
    if int(window_seconds) <= 0:
        raise ValueError(f"Invalid admission interval: {window_seconds}")
    if int(max_requests) < 0:
        raise ValueError(f"Invalid admission allowance: {max_requests}")


class SQLiteSharedRateRepository:
    """Atomic fixed-window admission. The cap guard lives inside the single
    upsert: two replicas racing for the last slot cannot both win. limit=0 is
    rejected BEFORE the insert, so a fresh identity cannot conjure a record."""

    def __init__(self, db):
        self.db = db

    async def admit(self, identifier: str, group: str, window_start: int,
                    max_requests: int, window_seconds: int = 60,
                    policy_version: str = "p3.v1") -> SharedAdmitResult:
        _validate_admission_inputs(max_requests, window_seconds)
        window_seconds = int(window_seconds)
        window_end = int(window_start) + window_seconds
        cleanup_after = window_end + RETENTION_WINDOWS * window_seconds
        if int(max_requests) == 0:
            # Explicit zero allowance: deny without creating a record (§3.5).
            return SharedAdmitResult(False, 0, int(window_start), window_end)

        cursor = await self.db.execute(
            """
            INSERT INTO rate_limits
                (identifier, group_name, window_seconds, window_start,
                 window_end, count, cleanup_after, policy_version)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(identifier, group_name, window_seconds, window_start)
            DO UPDATE SET count = rate_limits.count + 1
            WHERE rate_limits.count < ?
            """,
            (identifier, group, window_seconds, int(window_start),
             window_end, cleanup_after, policy_version, int(max_requests)),
        )
        await self.db.commit()
        admitted = cursor.rowcount > 0
        if admitted:
            row = await self.db.execute(
                "SELECT count FROM rate_limits WHERE identifier=? AND group_name=?"
                " AND window_seconds=? AND window_start=?",
                (identifier, group, window_seconds, int(window_start)),
            )
            fetched = await row.fetchone()
            count = int(fetched[0]) if fetched else 1
        else:
            count = int(max_requests)  # denied: allowance fully consumed
        return SharedAdmitResult(admitted, count, int(window_start), window_end)

    async def purge_expired(self, older_than_epoch: int) -> int:
        """Bounded housekeeping (§3.4 #7): sweep in batches so one tick cannot
        monopolize the database. Deletes where cleanup_after < cutoff — with
        cleanup_after always ≥ window_end + one window, an active counter is
        structurally undeletable. The cutoff is clamped to now so even a
        caller passing a bogus future epoch cannot sweep live windows."""
        cutoff = min(int(older_than_epoch), int(time.time()))
        batch = 500
        total = 0
        for _ in range(20):  # ≤ 10k rows per sweep; the ticker runs every minute
            cursor = await self.db.execute(
                """
                DELETE FROM rate_limits WHERE rowid IN (
                    SELECT rowid FROM rate_limits WHERE cleanup_after < ? LIMIT ?
                )
                """,
                (cutoff, batch),
            )
            await self.db.commit()
            swept = cursor.rowcount or 0
            total += swept
            if swept < batch:
                break
        return total


class FirestoreSharedRateRepository:
    """Transaction-based admission: read-guard-write inside one transaction.
    Doc ids include window_seconds so different interval configurations never
    share a counter. Native Firestore TTL is complementary, not authoritative:
    TTL deletion is asynchronous, so admission always interprets the window
    itself from the stored bounds."""

    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    @staticmethod
    def _doc_id(identifier: str, group: str, window_seconds: int,
                window_start: int) -> str:
        # Deterministic id; window bounds + retention keep the doc space bounded.
        return hashlib.sha256(
            f"{identifier}|{group}|{window_seconds}|{window_start}".encode()
        ).hexdigest()

    async def admit(self, identifier: str, group: str, window_start: int,
                    max_requests: int, window_seconds: int = 60,
                    policy_version: str = "p3.v1") -> SharedAdmitResult:
        from google.cloud import firestore
        from google.api_core.exceptions import AlreadyExists
        _validate_admission_inputs(max_requests, window_seconds)
        db = await self._get_db()
        window_seconds = int(window_seconds)
        window_end = int(window_start) + window_seconds
        cleanup_after = window_end + RETENTION_WINDOWS * window_seconds
        if int(max_requests) == 0:
            return SharedAdmitResult(False, 0, int(window_start), window_end)

        ref = db.collection("rate_limits").document(
            self._doc_id(identifier, group, window_seconds, int(window_start)))

        @firestore.async_transactional
        async def admit(transaction):
            snap = await ref.get(transaction=transaction)
            record = {
                "count": 1,
                "window_seconds": window_seconds,
                "window_start": int(window_start),
                "window_end": window_end,
                "cleanup_after": cleanup_after,
                "policy_version": policy_version,
            }
            if not snap.exists:
                transaction.create(ref, record)
                return SharedAdmitResult(True, 1, int(window_start), window_end)
            count = int((snap.to_dict() or {}).get("count") or 0)
            if count >= int(max_requests):
                return SharedAdmitResult(False, count, int(window_start), window_end)
            transaction.update(ref, {"count": count + 1})
            return SharedAdmitResult(True, count + 1, int(window_start), window_end)

        try:
            return await admit(db.transaction())
        except AlreadyExists:
            # Another replica won the create for this window — it consumed the
            # slot; this caller is denied the slot it raced for.
            return SharedAdmitResult(False, 1, int(window_start), window_end)

    async def purge_expired(self, older_than_epoch: int) -> int:
        db = await self._get_db()
        cutoff = min(int(older_than_epoch), int(time.time()))
        swept = 0
        while True:
            query = (db.collection("rate_limits")
                     .where("cleanup_after", "<", cutoff)
                     .limit(500))
            docs = [snap async for snap in query.stream()]
            if not docs:
                break
            for snap in docs:
                await snap.reference.delete()
                swept += 1
            if len(docs) < 500:
                break
        return swept


_repo: Optional[SharedRateRepository] = None


def set_shared_rate_repo(repo: Optional[SharedRateRepository]) -> None:
    global _repo
    _repo = repo


def get_shared_rate_repo() -> Optional[SharedRateRepository]:
    return _repo


# ── Observability (§3.20): counters only, labels never carry user ids ──

class _Metrics:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self._latencies: list[float] = []

    def incr(self, name: str, labels: Optional[dict[str, str]] = None, amount: int = 1) -> None:
        key = name
        if labels:
            flat = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if v is not None)
            key = f"{name}{{{flat}}}"
        self.counters[key] = self.counters.get(key, 0) + amount

    def observe_latency(self, seconds: float) -> None:
        self._latencies.append(seconds)
        if len(self._latencies) > 1000:
            del self._latencies[:500]

    def latency_ms(self) -> Optional[int]:
        """Recent shared-store admit latency (§3.20) — a climbing value is
        the early signal of store degradation before errors appear."""
        if not self._latencies:
            return None
        recent = self._latencies[-200:]
        return int(1000 * sum(recent) / len(recent))


METRICS = _Metrics()


def admission_metrics() -> dict:
    """Snapshot for the admin endpoint / alerting. The two load-bearing
    series: store_errors (outage detection) and local fallback activation."""
    out = {"counters": dict(METRICS.counters)}
    latency = METRICS.latency_ms()
    if latency is not None:
        out["store_admit_latency_ms_avg"] = latency
    return out


# ── Typed shared admission (§3.8/§3.9) ────────────────────────────────

async def shared_admission(identifier: str, group: str,
                           policy=None) -> AdmissionDecision:
    """Resolve ONE typed decision from the shared store.

    Enforcement semantics:
    - SHARED            — the durable store decided (authoritative cross-replica).
    - LOCAL_ONLY        — no shared store configured; the per-process limiter
                          is the documented enforcement layer.
    - LOCAL_EMERGENCY   — the store errored and this policy fails OPEN: admit,
                          but degraded=True so callers/metrics can tell that
                          global strictness was NOT enforced.
    - ADMISSION_UNAVAILABLE — the store errored and this policy fails CLOSED:
                          refuse with 503 rather than pretend quota was the cause.
    """
    from backend.security.admission_policy import resolve_policy
    if policy is None:
        policy = resolve_policy(group)

    decision = AdmissionDecision(
        allowed=True,
        enforcement=EnforcementMode.LOCAL_ONLY,
        policy_group=policy.group,
        policy_version=policy.policy_version,
        limit=policy.limit,
    )

    if not policy.shared:
        return decision  # in-process layer is authoritative; nothing to consult

    try:
        identity = validate_identity(identifier)
    except AdmissionPolicyError as exc:
        METRICS.incr("admission_denied", {"group": policy.group, "reason": "POLICY_INVALID"})
        return AdmissionDecision(
            allowed=False, reason=AdmissionReason.POLICY_INVALID,
            enforcement=EnforcementMode.LOCAL_ONLY,
            policy_group=policy.group, policy_version=policy.policy_version,
            limit=policy.limit, retry_after_seconds=0,
        )

    if policy.disabled:
        METRICS.incr("admission_denied", {"group": policy.group, "reason": "RATE_LIMITED"})
        return AdmissionDecision(
            allowed=False, reason=AdmissionReason.RATE_LIMITED,
            enforcement=EnforcementMode.DISABLED_BY_POLICY,
            policy_group=policy.group, policy_version=policy.policy_version,
            limit=0, remaining=0, retry_after_seconds=policy.window_seconds,
            reset_at_epoch=window_bounds(policy.window_seconds)[1],
            degraded=False,
        )

    repo = get_shared_rate_repo()
    if repo is None:
        return decision  # LOCAL_ONLY: per-process limiter still applies upstream

    start, end = window_bounds(policy.window_seconds)
    _t0 = time.perf_counter()
    try:
        result = await repo.admit(
            identity, policy.group, start, policy.limit,
            window_seconds=policy.window_seconds,
            policy_version=policy.policy_version,
        )
    except Exception:
        METRICS.observe_latency(time.perf_counter() - _t0)
        METRICS.incr("store_errors", {"group": policy.group})
        if policy.on_store_failure == StoreFailureMode.FAIL_CLOSED:
            logger.error("Shared admission store unavailable for %s; failing closed",
                         policy.group)
            METRICS.incr("admission_denied",
                         {"group": policy.group, "reason": "ADMISSION_UNAVAILABLE"})
            return AdmissionDecision(
                allowed=False, reason=AdmissionReason.ADMISSION_UNAVAILABLE,
                enforcement=EnforcementMode.LOCAL_EMERGENCY,
                policy_group=policy.group, policy_version=policy.policy_version,
                limit=policy.limit, remaining=0, retry_after_seconds=5,
                degraded=True,
            )
        logger.exception("Shared rate store unavailable for %s; degrading to "
                         "process-local emergency enforcement", policy.group)
        METRICS.incr("local_fallback_activations", {"group": policy.group})
        return AdmissionDecision(
            allowed=True, enforcement=EnforcementMode.LOCAL_EMERGENCY,
            policy_group=policy.group, policy_version=policy.policy_version,
            limit=policy.limit, remaining=policy.limit,
            reset_at_epoch=end, degraded=True,
        )

    decision.enforcement = EnforcementMode.SHARED
    METRICS.observe_latency(time.perf_counter() - _t0)
    decision.reset_at_epoch = result.window_end
    decision.remaining = max(0, policy.limit - result.count)
    decision.degraded = False
    if not result.allowed:
        decision.allowed = False
        decision.reason = AdmissionReason.RATE_LIMITED
        decision.remaining = 0
        decision.retry_after_seconds = max(1, result.window_end - int(time.time()) + 1)
        METRICS.incr("admission_denied", {"group": policy.group, "reason": "RATE_LIMITED"})
    else:
        METRICS.incr("admitted", {"group": policy.group})
    return decision


async def shared_admit(identifier: str, group: str, window_seconds: int,
                       max_requests: int) -> Optional[bool]:
    """Compat shim for pre-Phase-3 callers: True/False decided, None when no
    shared store is configured. New code uses shared_admission()."""
    repo = get_shared_rate_repo()
    if repo is None:
        return None
    try:
        result = await repo.admit(identifier, group, current_window(window_seconds),
                                  max_requests, window_seconds=window_seconds)
        return result.allowed
    except Exception:
        logger.exception("Shared rate store unavailable; falling back to process-local")
        return None
