"""Budget service (Phase 2, §2.10 / §2.13 / §2.19 / §2.20-11).

Coordinates policy resolution, estimation, reservation, dispatch intent,
settlement, uncertainty, and reconciliation for EVERY paid provider call.
Provider transport and repository persistence stay outside this service —
callers wrap their dispatch in `metered_call` and feed back usage receipts.

Failure posture (§2.13):
- Missing/unavailable accounting blocks new paid execution (fail closed).
- Exceptions before dispatch release the hold.
- Exceptions after dispatch (timeout, disconnect, crash) mark the reservation
  UNCERTAIN — the hold stays counted until evidence resolves it.
- Application-level parsing failures still settle the model usage: the call
  happened even if the answer was unusable.
"""
from __future__ import annotations

import hashlib
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from backend.config.settings import settings
from backend.services.pricing import compute_cost
from backend.models.usage import (
    BudgetError,
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
    resolve_effective_limit,
    utc_period_day,
)
from backend.repositories.usage_repository import (
    BucketLimit,
    get_usage_repo,
)
from backend.services.usage_normalizer import (
    NormalizedUsage,
    estimate_units_from_text,
)

logger = logging.getLogger(__name__)


# ── Observability (§2.19) ─────────────────────────────────────────────
# Counters only; labels carry provider/model/scope/category — never prompts,
# credentials, or raw user identifiers. Snapshot via budget_metrics().

class _Metrics:
    def __init__(self) -> None:
        self.counters: Dict[str, int] = {}
        self._latencies: List[float] = []

    def incr(self, name: str, labels: Optional[Dict[str, str]] = None, amount: int = 1) -> None:
        key = name
        if labels:
            flat = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if v is not None)
            key = f"{name}{{{flat}}}"
        self.counters[key] = self.counters.get(key, 0) + amount

    def observe_latency(self, name: str, seconds: float) -> None:
        self._latencies.append(seconds)
        if len(self._latencies) > 1000:
            del self._latencies[:500]

    def latency_ms(self, name: str) -> Optional[int]:
        if not self._latencies:
            return None
        recent = self._latencies[-200:]
        return int(1000 * sum(recent) / len(recent))


METRICS = _Metrics()


def budget_metrics() -> Dict[str, Any]:
    return {
        "counters": dict(METRICS.counters),
        "reservation_latency_ms_avg": METRICS.latency_ms("reservation"),
    }


class AccountingUnavailableError(BudgetError):
    """The repository is missing or failed — paid work must be blocked (G7),
    never admitted for free."""

    def __init__(self, message: str = "Budget accounting is unavailable; new paid work is paused"):
        super().__init__(BudgetErrorCode.ACCOUNTING_UNAVAILABLE, message)


# ── Policy resolution (§2.3) ──────────────────────────────────────────


async def _user_daily_limit(settings_repo, user_id: str) -> int:
    user_limit: Optional[int] = None
    if settings_repo is not None and user_id:
        saved = await settings_repo.get_settings(user_id) or {}
        raw = saved.get("daily_token_limit")
        if raw is not None:
            user_limit = int(raw)
    return resolve_effective_limit(
        user_limit=user_limit,
        platform_limit=settings.PLATFORM_DAILY_TOKEN_LIMIT,
        default_limit=settings.DEFAULT_DAILY_TOKEN_LIMIT,
    )


async def resolve_buckets(
    settings_repo, user_id: str, organization_id: Optional[str] = None,
    repo=None,
) -> List[BucketLimit]:
    """The applicable constraints for one caller: user allowance + platform
    ceiling (+ org bucket when the run is organization-owned). A call must
    satisfy every one of them (§2.3)."""
    if not user_id:
        raise AccountingUnavailableError("No payer resolved for a paid call")
    repo = repo or get_usage_repo()
    if repo is None:
        raise AccountingUnavailableError()
    buckets: List[BucketLimit] = []
    try:
        user_limit = await _user_daily_limit(settings_repo, user_id)
    except BudgetError:
        raise
    except Exception as exc:
        raise AccountingUnavailableError("Could not load budget policy") from exc
    buckets.append(BucketLimit(BudgetScope.USER, user_id, user_limit))
    platform_limit = settings.PLATFORM_DAILY_TOKEN_LIMIT
    if platform_limit is not None:
        buckets.append(
            BucketLimit(BudgetScope.PLATFORM, "platform", int(platform_limit))
        )
    if organization_id:
        # Org allowance uses the platform default until org-level settings
        # exist; the bucket itself is already enforced per (org, period).
        buckets.append(
            BucketLimit(
                BudgetScope.ORG, organization_id,
                int(platform_limit or settings.DEFAULT_DAILY_TOKEN_LIMIT),
            )
        )
    return buckets


def validate_daily_token_limit(value: int, *, platform_ceiling: Optional[int] = None) -> int:
    """Settings semantics (§2.3): null→default, 0→no new paid usage,
    negative→rejected, oversized→rejected, values above an
    administrator-controlled ceiling clamp DOWN."""
    if value is None:
        return settings.DEFAULT_DAILY_TOKEN_LIMIT
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise BudgetError(
            BudgetErrorCode.BUDGET_POLICY_INVALID,
            "daily_token_limit must be an integer",
        ) from exc
    if value < 0:
        raise BudgetError(
            BudgetErrorCode.BUDGET_POLICY_INVALID,
            "daily_token_limit cannot be negative (0 disables new paid usage)",
        )
    ceiling = platform_ceiling or 10_000_000_000
    if value > ceiling:
        raise BudgetError(
            BudgetErrorCode.BUDGET_POLICY_INVALID,
            f"daily_token_limit exceeds the platform ceiling ({ceiling})",
        )
    return value


# ── Metered execution boundary (§2.10/§2.13) ──────────────────────────


@dataclass
class MeteredCall:
    """Handle a provider caller uses to report what happened. Exactly one of
    settle/uncertain/release runs at context exit."""

    reservation: Optional[Reservation]
    user_id: str
    provider: str
    model: Optional[str]
    purpose: str
    dispatched: bool = False
    usage: Optional[NormalizedUsage] = None
    response_text_length: int = 0
    _finished: bool = False
    _service: Optional["BudgetService"] = None
    rejection_reason: Optional[str] = None
    notes: Dict[str, Any] = field(default_factory=dict)

    async def mark_dispatched(self) -> None:
        """Call immediately before sending the provider request. Commits
        dispatch intent durably (§2.6): from here a crash or timeout may
        leave provider-side uncertainty, so failures no longer release the
        hold — before this point, failures release it."""
        if self._service is not None and self.reservation is not None:
            await self._service.mark_dispatching(self.reservation.reservation_id)
        self.dispatched = True

    def note_provider_rejection(self, reason: str) -> None:
        """The provider definitively rejected the request WITHOUT generating
        (quota 429, auth 401/403, unknown model 404): stronger evidence than
        a timeout, so the hold is released on exit instead of parked as
        uncertain (§2.13)."""
        self.rejection_reason = reason

    def capture_usage(self, usage: NormalizedUsage) -> None:
        """Capture provider usage metadata — call this BEFORE application
        parsing so a JSON failure still settles the model usage (§2.10).

        §17.2 (finding D): a multi-turn runner execution emits MULTIPLE
        usage events (one per model invocation, including tool-driven
        follow-ups). Events ACCUMULATE per provider semantics — input
        tokens sum, output tokens sum — neither replacing the previous
        value nor double-counting a cumulative carrier."""
        if usage is None:
            return
        if self.usage is None:
            self.usage = usage
            return
        try:
            def _num(v):
                return int(v or 0)
            self.usage = NormalizedUsage(
                provider=usage.provider or self.usage.provider,
                requested_model=usage.requested_model
                or self.usage.requested_model,
                resolved_model=usage.resolved_model
                or self.usage.resolved_model,
                provider_request_id=usage.provider_request_id
                or self.usage.provider_request_id,
                input_tokens=_num(self.usage.input_tokens)
                + _num(usage.input_tokens),
                output_tokens=_num(self.usage.output_tokens)
                + _num(usage.output_tokens),
                cached_input_tokens=_num(self.usage.cached_input_tokens)
                + _num(usage.cached_input_tokens),
                reasoning_tokens=_num(self.usage.reasoning_tokens)
                + _num(usage.reasoning_tokens),
                reported_total_tokens=(
                    _num(self.usage.reported_total_tokens)
                    + _num(usage.reported_total_tokens)),
                measurement_source=usage.measurement_source,
                raw_metadata=usage.raw_metadata
                or self.usage.raw_metadata,
            )
        except Exception:
            # A shape we cannot sum: keep the LATEST event (never lose
            # all accounting) — the settle path still normalizes.
            self.usage = usage

    def capture_response_text(self, text: Optional[str]) -> None:
        if text:
            self.response_text_length += len(text)


async def _finish_metered_call(service: "BudgetService", call: MeteredCall,
                               exc: Optional[BaseException]) -> None:
    if call._finished or call.reservation is None:
        return
    call._finished = True
    reservation_id = call.reservation.reservation_id
    try:
        if call.usage is not None and call.usage.known:
            # Provider reported usage (or a known-unknown when nothing was
            # reported) — settle with what we have; never clip overruns.
            units = call.usage.policy_units()
            if units == 0 and call.usage.measurement_source == "unknown":
                units = service._fallback_estimate(call)
            # 30.32: money = measured quantities × the versioned tariff.
            price = compute_cost(call.usage, model=call.usage.resolved_model
                                 or call.model)
            await service.settle(
                reservation_id,
                SettlementReceipt(
                    reservation_id=reservation_id,
                    units=units,
                    measurement_source=(
                        MeasurementSource.PROVIDER_REPORTED
                        if call.usage.measurement_source == "provider_reported"
                        else MeasurementSource.ESTIMATED
                    ),
                    pricing_version=price.pricing_version,
                    cost_amount=price.amount,
                    cost_currency=price.currency,
                    **call.usage.to_receipt_kwargs(),
                ),
            )
            return
        if exc is None:
            # Dispatch completed without usage metadata: settle an estimate
            # labeled as such (§2.12 — estimation is not measurement).
            units = service._fallback_estimate(call)
            await service.settle(
                reservation_id,
                SettlementReceipt(
                    reservation_id=reservation_id, units=units,
                    measurement_source=MeasurementSource.ESTIMATED,
                    resolved_model=call.model,
                    normalizer_version="estimate-fallback",
                ),
            )
            return
        if not call.dispatched:
            await service.release_unused(reservation_id, evidence={
                "phase": "pre_dispatch", "error": type(exc).__name__,
            })
            return
        if call.rejection_reason:
            # Provider refused the request without generating (429/401/404):
            # proven unused, release with evidence (§2.13).
            await service.release_unused(reservation_id, evidence={
                "phase": "provider_rejection", "reason": call.rejection_reason,
            })
            return
        # Dispatched, then failed, without usage evidence: a timeout or a
        # disconnect is NOT proof the provider did nothing (§2.6/§2.13).
        await service.mark_uncertain(reservation_id, reason=type(exc).__name__)
    except BudgetError as settle_err:
        METRICS.incr("budget_settlement_failures", {"category": settle_err.code.value})
        logger.error(
            "Settlement failed for reservation %s (%s); hold preserved for reconciliation",
            reservation_id, settle_err.code.value,
        )
    except Exception:
        METRICS.incr("budget_settlement_failures", {"category": "unexpected"})
        logger.exception("Unexpected settlement failure for reservation %s", reservation_id)


class BudgetService:
    """Single coordination point for budget admission and settlement."""

    def __init__(self, repo=None):
        self._repo = repo

    @property
    def repo(self):
        repo = self._repo or get_usage_repo()
        if repo is None:
            raise AccountingUnavailableError()
        return repo

    def accounting_available(self) -> bool:
        return (self._repo or get_usage_repo()) is not None

    # ── reservation ───────────────────────────────────────────────────

    async def reserve(
        self,
        *,
        user_id: str,
        estimated_units: int,
        provider: str = "gemini",
        model: Optional[str] = None,
        purpose: str = "generation",
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        call_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        request_key: Optional[str] = None,
        settings_repo=None,
    ) -> Optional[Reservation]:
        """Admit one potentially billable attempt. Returns None only in
        observe mode (§2.18 mixed-version rollout), where accounting problems
        or refusals are recorded as unmetered dispatches instead of blocking —
        'enforce' (default) never returns None."""
        observe = settings.BUDGET_ENFORCEMENT_MODE == "observe"
        try:
            buckets = await resolve_buckets(settings_repo, user_id, organization_id, repo=self.repo)
            request = ReservationRequest(
                user_id=user_id,
                units=int(estimated_units),
                unit=BudgetUnit.TOKENS,
                organization_id=organization_id,
                run_id=run_id,
                task_id=task_id,
                call_id=call_id,
                attempt_id=attempt_id,
                provider=provider,
                model=model,
                purpose=purpose,
                request_key=request_key,
            )
            started = time.monotonic()
            # Separate guard on open holds (§2.14): repeated unresolved calls must
            # not accumulate unlimited exposure even when the daily limit is large.
            ratio = settings.MAX_OUTSTANDING_HOLD_RATIO
            if ratio < 1.0:
                user_bucket = next(b for b in buckets if b.scope == BudgetScope.USER)
                snapshot = await self.repo.get_budget_snapshot(
                    BudgetScope.USER, user_id, utc_period_day(), user_bucket.limit
                )
                if snapshot.reserved + int(estimated_units) > int(
                    user_bucket.limit * ratio
                ):
                    METRICS.incr("budget_admissions", {"outcome": "rejected",
                                                       "provider": provider})
                    raise BudgetError(
                        BudgetErrorCode.BUDGET_EXHAUSTED,
                        "outstanding reservation exposure exceeds the allowed share of the daily limit",
                        available=max(0, int(user_bucket.limit * ratio) - snapshot.reserved),
                        required=int(estimated_units),
                        retryable_now=False,
                        details={"scope": "outstanding_holds"},
                    )
            reservation = await self.repo.reserve(request, buckets)
        except AccountingUnavailableError:
            if observe:
                METRICS.incr("budget_unmetered_dispatches", {"reason": "accounting_unavailable"})
                return None
            raise
        except BudgetError as exc:
            if exc.code == BudgetErrorCode.BUDGET_EXHAUSTED:
                METRICS.incr("budget_admissions", {"outcome": "rejected",
                                                   "provider": provider})
            if observe and exc.code in {
                BudgetErrorCode.BUDGET_EXHAUSTED, BudgetErrorCode.BUDGET_POLICY_INVALID,
            }:
                METRICS.incr("budget_unmetered_dispatches", {"reason": exc.code.value})
                return None
            raise
        METRICS.incr("budget_admissions", {"outcome": "admitted", "provider": provider})
        METRICS.observe_latency("reservation", time.monotonic() - started)
        return reservation

    # ── state transitions ─────────────────────────────────────────────

    async def mark_dispatching(self, reservation_id: str,
                               expected_version: Optional[int] = None) -> Reservation:
        return await self.repo.mark_dispatching(reservation_id, expected_version)

    async def settle(self, reservation_id: str, receipt: SettlementReceipt) -> Reservation:
        reservation = await self.repo.settle(reservation_id, receipt)
        if receipt.units > (reservation.units or 0):
            METRICS.incr("budget_overruns", {"provider": reservation.provider})
        METRICS.incr("budget_settlements", {"provider": reservation.provider})
        return reservation

    async def reconcile_conflicting_receipt(
        self, reservation_id: str, receipt: SettlementReceipt, *,
        reason: str = "conflicting provider receipt", actor: str = "system",
    ) -> Dict[str, Any]:
        """Settlement CONFLICT reconciliation (Phase 30 / 30.8).

        A final receipt that disagrees with the one already settled never
        overwrites the original: the original settled units stay as the
        booked value, and the delta is moved through the audited,
        idempotent correction path so the ledger converges on the
        corrected total while both receipts remain inspectable. Two
        reconcilers processing the same late receipt produce the SAME
        correction id — the second application is a no-op."""
        original = await self.repo.get_reservation(reservation_id)
        if original is None or original.state != ReservationState.SETTLED:
            raise BudgetError(
                BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                "conflicting receipt requires a settled reservation",
            )
        delta = int(receipt.units) - int(original.settled_units or 0)
        correction_id = (
            "corr:" + hashlib.sha256(
                f"{reservation_id}|{original.receipt_digest or ''}|{receipt.digest()}"
                .encode("utf-8")).hexdigest()[:32]
        )
        if delta != 0:
            await self.repo.record_adjustment(
                scope=BudgetScope.USER,
                subject_id=original.user_id,
                period=original.period,
                units=delta,
                reason=f"{reason}; original_receipt_digest={original.receipt_digest} "
                       f"conflicting_receipt_digest={receipt.digest()}",
                actor=actor,
                correction_id=correction_id,
            )
            METRICS.incr("budget_settlement_conflicts_reconciled", {"provider": original.provider})
        else:
            METRICS.incr("budget_settlement_conflicts_idempotent", {"provider": original.provider})
        return {
            "reservation_id": reservation_id,
            "correction_id": correction_id,
            "original_settled_units": int(original.settled_units or 0),
            "original_receipt_digest": original.receipt_digest,
            "conflicting_receipt_digest": receipt.digest(),
            "conflicting_units": int(receipt.units),
            "delta": delta,
            "resolved": True,
        }

    async def release_unused(self, reservation_id: str,
                             evidence: Optional[Dict[str, Any]] = None) -> Reservation:
        METRICS.incr("budget_releases")
        return await self.repo.release_unused(reservation_id, evidence)

    async def mark_uncertain(self, reservation_id: str, reason: str) -> Reservation:
        METRICS.incr("budget_uncertain_marked", {"reason": reason[:40]})
        return await self.repo.mark_uncertain(reservation_id, reason)

    def _fallback_estimate(self, call: MeteredCall) -> int:
        input_estimate = call.notes.get("input_estimate") or 0
        output_estimate = (call.response_text_length + 3) // 4
        return int(input_estimate) + int(output_estimate)

    # ── metered execution context ─────────────────────────────────────

    @asynccontextmanager
    async def metered_call(
        self,
        *,
        user_id: str,
        estimated_input_units: int,
        output_reserve_units: Optional[int] = None,
        provider: str = "gemini",
        model: Optional[str] = None,
        purpose: str = "generation",
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        call_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        request_key: Optional[str] = None,
        settings_repo=None,
    ) -> AsyncIterator[MeteredCall]:
        """Reserve → (caller dispatches) → settle/uncertain/release.

        estimated_input_units must cover the WHOLE request the provider sees:
        system instructions, history, tool definitions/results, memory, and
        multimodal inputs (§2.12). The hold adds a bounded output reserve.
        The caller MUST `await call.mark_dispatched()` immediately before
        sending the provider request; failures before that point release the
        hold, failures after mark it uncertain.
        """
        reserve_units = int(estimated_input_units) + int(
            output_reserve_units
            if output_reserve_units is not None
            else settings.MAX_OUTPUT_RESERVE_TOKENS
        )
        # §4.0C (R4): admission resolves the user's SAVED policy — the
        # process default installed at factory init — unless the caller
        # supplied a repository explicitly. Without this, provider calls
        # admitted against the DEFAULT limit while the UI showed the saved one.
        effective_settings_repo = settings_repo or get_default_settings_repo()
        reservation = await self.reserve(
            user_id=user_id, estimated_units=reserve_units,
            provider=provider, model=model, purpose=purpose, run_id=run_id,
            task_id=task_id, call_id=call_id, attempt_id=attempt_id,
            organization_id=organization_id, request_key=request_key,
            settings_repo=effective_settings_repo,
        )
        if reservation is not None and reservation.state in {
            ReservationState.SETTLED, ReservationState.RELEASED,
        }:
            # Replay of an already-finished logical call: the contract returns
            # the existing record, but a caller about to dispatch needs a NEW
            # attempt identity — never dispatch under a terminal reservation.
            METRICS.incr("budget_replays_terminal")
            reservation = await self.reserve(
                user_id=user_id, estimated_units=reserve_units,
                provider=provider, model=model, purpose=purpose, run_id=run_id,
                task_id=task_id, call_id=call_id,
                attempt_id=attempt_id or new_attempt_id(),
                organization_id=organization_id,
                request_key=f"{reservation.request_key}|{new_attempt_id()}",
                settings_repo=settings_repo,
            )
        call = MeteredCall(
            reservation=reservation, user_id=user_id, provider=provider,
            model=model, purpose=purpose,
            notes={"input_estimate": int(estimated_input_units)},
            _service=self,
        )
        try:
            yield call
        except BaseException as exc:
            await _finish_metered_call(self, call, exc)
            raise
        else:
            await _finish_metered_call(self, call, None)

    # ── snapshots / admission checks (§2.15) ──────────────────────────

    async def get_user_snapshot(
        self, user_id: str, settings_repo=None, *,
        organization_id: Optional[str] = None,
        period: Optional[str] = None,
    ) -> BudgetSnapshot:
        repo = self.repo  # raises AccountingUnavailableError when missing
        period = period or utc_period_day()
        try:
            limit = await _user_daily_limit(settings_repo, user_id)
        except BudgetError:
            raise
        except Exception as exc:
            raise AccountingUnavailableError("Could not load budget policy") from exc
        snapshot = await repo.get_budget_snapshot(
            BudgetScope.USER, user_id, period, limit
        )
        return _attach_provider_rate_limit(snapshot, user_id)

    async def get_platform_snapshot(self, period: Optional[str] = None) -> BudgetSnapshot:
        repo = self.repo
        period = period or utc_period_day()
        limit = int(settings.PLATFORM_DAILY_TOKEN_LIMIT or settings.DEFAULT_DAILY_TOKEN_LIMIT)
        return await repo.get_budget_snapshot(BudgetScope.PLATFORM, "platform", period, limit)

    async def check_admission(
        self, user_id: str, required_units: int, settings_repo=None, *,
        organization_id: Optional[str] = None,
    ) -> BudgetSnapshot:
        """Admission probe: raises BUDGET_EXHAUSTED with available/required
        when the caller cannot afford `required_units`; returns the snapshot
        otherwise. Accounting unavailability raises (fail closed)."""
        buckets = await resolve_buckets(settings_repo, user_id, organization_id, repo=self.repo)
        period = utc_period_day()
        repo = self.repo
        worst: Optional[BudgetSnapshot] = None
        for bucket in buckets:
            snapshot = await repo.get_budget_snapshot(
                bucket.scope, bucket.subject_id, period, bucket.limit
            )
            if worst is None or snapshot.available < worst.available:
                worst = snapshot
            if snapshot.available < required_units:
                METRICS.incr("budget_admission_probes", {"outcome": "exhausted"})
                raise BudgetError(
                    BudgetErrorCode.BUDGET_EXHAUSTED,
                    f"budget exhausted for scope={bucket.scope.value}",
                    available=max(0, snapshot.available),
                    required=required_units,
                    retryable_now=False,
                    details={"scope": bucket.scope.value},
                )
        METRICS.incr("budget_admission_probes", {"outcome": "ok"})
        assert worst is not None
        return worst

    # ── reconciliation (§2.13/§2.19, §2.20-11) ─────────────────────────

    async def sweep_for_reconciliation(self, *, older_than_minutes: int = 60,
                                       limit: int = 50) -> Dict[str, Any]:
        """Reservation expiry triggers INVESTIGATION, not unconditional
        release (§2.6): stale reserved/dispatching holds are marked uncertain
        (still counted); genuinely stale uncertain holds stay for evidence.
        Returns a report for the ops runbook."""
        repo = self.repo
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
        candidates = await repo.list_reconciliation_candidates(
            older_than=cutoff,
            states=[ReservationState.RESERVED, ReservationState.DISPATCHING],
            limit=limit,
        )
        report = {"investigated": 0, "marked_uncertain": 0, "ids": []}
        for reservation in candidates:
            report["investigated"] += 1
            report["ids"].append(reservation.reservation_id)
            try:
                await repo.mark_uncertain(
                    reservation.reservation_id,
                    reason=f"reconciliation sweep (stale {reservation.state.value})",
                )
                report["marked_uncertain"] += 1
            except BudgetError as exc:
                logger.info(
                    "Reconciliation skip %s: %s", reservation.reservation_id, exc.code.value
                )
        if report["investigated"]:
            METRICS.incr("budget_reconciliation_sweeps", amount=1)
            METRICS.incr("budget_reconciliation_investigated",
                         amount=report["investigated"])
        return report


def _attach_provider_rate_limit(snapshot: BudgetSnapshot, user_id: str) -> BudgetSnapshot:
    """30.32: surface the latest ACTUAL provider rate-limit observation
    (headers/receipts) on the snapshot; absent stays absent (the UI
    renders an explicit unavailable state)."""
    try:
        from backend.services.provider_rate_limit import latest_observation
        observation = latest_observation(user_id)
    except Exception:
        observation = None
    if observation:
        snapshot.provider_rate_limit = observation
    return snapshot


_default_service: Optional[BudgetService] = None

# §4.0C (R4): the saved-policy repository every provider boundary consults.
# Installed at factory init so metered_call resolves the user's SAVED daily
# limit even when the caller cannot reach a settings repository — the UI
# snapshot and execution admission then resolve the SAME policy. The explicit
# per-call parameter still wins when a caller supplies one.
_DEFAULT_SETTINGS_REPO = None


def set_default_settings_repo(repo) -> None:
    global _DEFAULT_SETTINGS_REPO
    _DEFAULT_SETTINGS_REPO = repo


def get_default_settings_repo():
    return _DEFAULT_SETTINGS_REPO


def get_budget_service() -> BudgetService:
    global _default_service
    if _default_service is None:
        _default_service = BudgetService()
    return _default_service


def set_budget_service(service: Optional[BudgetService]) -> None:
    global _default_service
    _default_service = service
