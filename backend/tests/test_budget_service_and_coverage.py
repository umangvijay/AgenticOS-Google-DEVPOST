"""Phase 2 budget service tests (§2.10/§2.13/§2.15).

Covers the metered execution lifecycle (reserve → dispatch → settle/release/
uncertain), fail-closed accounting, admission probes, and reconciliation
sweeps. Also covers §2.18: unmetered legacy recording must not double count
with reservation settlement.
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.config.settings import settings as app_settings
from backend.models.usage import (
    BudgetError,
    BudgetErrorCode,
    BudgetScope,
    MeasurementSource,
    ReservationState,
    SettlementReceipt,
)
from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.usage_repository import SQLiteUsageRepository, set_usage_repo
from backend.services import budget_service as bs
from backend.services.budget_service import (
    AccountingUnavailableError,
    BudgetService,
    budget_metrics,
)
from backend.services.usage_normalizer import (
    NormalizedUsage,
    estimate_units_from_text,
    normalize_gemini_usage,
    normalize_openai_usage,
)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


class FixedSettings:
    def __init__(self, limit=1000):
        self._limit = limit

    async def get_settings(self, user_id):
        return {"daily_token_limit": self._limit}


@pytest_asyncio.fixture
async def env(tmp_path):
    manager = DatabaseManager(str(tmp_path / "svc.db"))
    await manager.initialize()
    repo = SQLiteUsageRepository(manager)
    service = BudgetService(repo=repo)
    yield manager, repo, service
    await manager.close()


# ── metered lifecycle ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_call_reserves_then_settles_actual(env):
    _manager, repo, service = env
    async with service.metered_call(
        user_id="m1", estimated_input_units=100, output_reserve_units=200,
        settings_repo=FixedSettings(1000),
    ) as call:
        await call.mark_dispatched()
        call.capture_usage(NormalizedUsage(
            provider="gemini", input_tokens=90, output_tokens=40,
            reported_total_tokens=130, measurement_source="provider_reported",
        ))
    reservation = await repo.get_reservation(call.reservation.reservation_id)
    assert reservation.state == ReservationState.SETTLED
    snapshot = await service.get_user_snapshot("m1", FixedSettings(1000))
    assert snapshot.consumed == 130, "policy units must use the reported total"
    assert snapshot.reserved == 0


@pytest.mark.asyncio
async def test_provider_error_before_dispatch_releases(env):
    _manager, repo, service = env
    with pytest.raises(RuntimeError):
        async with service.metered_call(
            user_id="m2", estimated_input_units=100, output_reserve_units=100,
            settings_repo=FixedSettings(1000),
        ) as call:
            raise RuntimeError("client-side validation failed before any send")
    reservation = await repo.get_reservation(call.reservation.reservation_id)
    assert reservation.state == ReservationState.RELEASED
    snapshot = await service.get_user_snapshot("m2", FixedSettings(1000))
    assert snapshot.consumed == 0 and snapshot.reserved == 0


@pytest.mark.asyncio
async def test_timeout_after_dispatch_marks_uncertain_not_free(env):
    _manager, repo, service = env
    with pytest.raises(TimeoutError):
        async with service.metered_call(
            user_id="m3", estimated_input_units=100, output_reserve_units=100,
            settings_repo=FixedSettings(1000),
        ) as call:
            await call.mark_dispatched()
            raise TimeoutError("provider timed out after transmission")
    reservation = await repo.get_reservation(call.reservation.reservation_id)
    assert reservation.state == ReservationState.UNCERTAIN
    snapshot = await service.get_user_snapshot("m3", FixedSettings(1000))
    assert snapshot.reserved == 200, "timeout released the allowance for free"


@pytest.mark.asyncio
async def test_invalid_json_still_settles_model_usage(env):
    """§2.10 ordering: capture accounting even when application parsing
    fails; the model call happened."""
    _manager, repo, service = env
    with pytest.raises(ValueError):
        async with service.metered_call(
            user_id="m4", estimated_input_units=100, output_reserve_units=100,
            settings_repo=FixedSettings(1000),
        ) as call:
            await call.mark_dispatched()
            call.capture_usage(NormalizedUsage(
                provider="gemini", input_tokens=100, output_tokens=250,
                reported_total_tokens=350, measurement_source="provider_reported",
            ))
            raise ValueError("provider returned invalid JSON")
    reservation = await repo.get_reservation(call.reservation.reservation_id)
    assert reservation.state == ReservationState.SETTLED
    snapshot = await service.get_user_snapshot("m4", FixedSettings(1000))
    assert snapshot.consumed == 350


@pytest.mark.asyncio
async def test_success_without_usage_metadata_settles_estimate(env):
    """No usage metadata ≠ free: an estimate is settled, labeled as such."""
    _manager, repo, service = env
    async with service.metered_call(
        user_id="m5", estimated_input_units=200, output_reserve_units=0,
        settings_repo=FixedSettings(10000),
    ) as call:
        await call.mark_dispatched()
        call.capture_response_text("x" * 400)  # 100 estimated tokens
    reservation = await repo.get_reservation(call.reservation.reservation_id)
    assert reservation.state == ReservationState.SETTLED
    assert reservation.measurement_source == MeasurementSource.ESTIMATED.value
    snapshot = await service.get_user_snapshot("m5", FixedSettings(10000))
    assert snapshot.consumed == 200 + 100


@pytest.mark.asyncio
async def test_missing_accounting_blocks_paid_execution(env, monkeypatch):
    """G7: no repository → fail closed, never admit for free."""
    set_usage_repo(None)
    try:
        open_service = BudgetService(repo=None)
        with pytest.raises(AccountingUnavailableError):
            async with open_service.metered_call(
                user_id="m6", estimated_input_units=10,
            ) as call:
                pass
    finally:
        pass  # fixture repo never installed process-wide


@pytest.mark.asyncio
async def test_budget_exhausted_blocks_dispatch(env, monkeypatch):
    _manager, _repo, service = env
    # Disable the outstanding-hold guard so this test exercises the cap itself.
    monkeypatch.setattr(app_settings, "MAX_OUTSTANDING_HOLD_RATIO", 1.0, raising=False)
    with pytest.raises(BudgetError) as exc:
        async with service.metered_call(
            user_id="m7", estimated_input_units=500, output_reserve_units=600,
            settings_repo=FixedSettings(1000),
        ):
            pass
    assert exc.value.code == BudgetErrorCode.BUDGET_EXHAUSTED
    assert exc.value.available == 1000 and exc.value.required == 1100


@pytest.mark.asyncio
async def test_platform_ceiling_bounds_user_allowance(env, monkeypatch):
    _manager, _repo, service = env
    monkey_limit = 500
    original = app_settings.PLATFORM_DAILY_TOKEN_LIMIT
    monkeypatch.setattr(app_settings, "MAX_OUTSTANDING_HOLD_RATIO", 1.0, raising=False)
    app_settings.PLATFORM_DAILY_TOKEN_LIMIT = monkey_limit
    try:
        with pytest.raises(BudgetError):
            # User settings allow 5000, but the platform ceiling is 500 and
            # the reservation asks for more than that.
            async with service.metered_call(
                user_id="m8", estimated_input_units=400, output_reserve_units=200,
                settings_repo=FixedSettings(5000),
            ):
                pass
    finally:
        app_settings.PLATFORM_DAILY_TOKEN_LIMIT = original


@pytest.mark.asyncio
async def test_replayed_execution_message_returns_same_reservation(env):
    """§2.5 replay: a repeated admission for the same logical call returns
    the EXISTING reservation (no second hold) — at the repository layer."""
    _manager, repo, service = env
    first = await service.reserve(
        user_id="m9", estimated_units=100, call_id="same-call",
        settings_repo=FixedSettings(1000),
    )
    second = await service.reserve(
        user_id="m9", estimated_units=100, call_id="same-call",
        settings_repo=FixedSettings(1000),
    )
    assert first.reservation_id == second.reservation_id
    snapshot = await service.get_user_snapshot("m9", FixedSettings(1000))
    assert snapshot.reserved == 100, "replay created a second hold"


@pytest.mark.asyncio
async def test_redispatch_after_completion_is_metered_as_new_attempt(env):
    """§2.5 key discipline: a GENUINE new provider attempt (a re-executed
    call that actually dispatches again) gets a new attempt identity and its
    own reservation — dispatch is never unmetered."""
    _manager, repo, service = env
    ids = []
    for _ in range(2):
        async with service.metered_call(
            user_id="m9b", estimated_input_units=100, output_reserve_units=0,
            call_id="same-call", settings_repo=FixedSettings(10000),
        ) as call:
            await call.mark_dispatched()
            call.capture_usage(NormalizedUsage(
                provider="gemini", reported_total_tokens=100,
                measurement_source="provider_reported",
            ))
        ids.append(call.reservation.reservation_id)
    assert ids[0] != ids[1], "re-dispatch reused a terminal reservation"
    snapshot = await service.get_user_snapshot("m9b", FixedSettings(10000))
    assert snapshot.consumed == 200, "both attempts were really paid for"


# ── admission probe (§2.15) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_admission_reports_available_and_required(env):
    _manager, _repo, service = env
    snapshot = await service.check_admission("probe-user", 400, FixedSettings(1000))
    assert snapshot.available == 1000
    # Fill the budget with a hold (within the 0.5 exposure guard), then probe.
    await service.reserve(
        user_id="probe-user", estimated_units=400, settings_repo=FixedSettings(1000),
    )
    with pytest.raises(BudgetError) as exc:
        await service.check_admission("probe-user", 700, FixedSettings(1000))
    assert exc.value.code == BudgetErrorCode.BUDGET_EXHAUSTED
    assert exc.value.available == 600 and exc.value.required == 700
    assert exc.value.retryable_now is False


# ── reconciliation (§2.13/§2.20-11) ───────────────────────────────────


@pytest.mark.asyncio
async def test_reconciliation_sweep_marks_stale_dispatching_uncertain(env):
    _manager, repo, service = env
    reservation = await service.reserve(
        user_id="sweep-user", estimated_units=100, settings_repo=FixedSettings(1000),
    )
    await service.mark_dispatching(reservation.reservation_id)
    report = await service.sweep_for_reconciliation(older_than_minutes=0)
    assert report["investigated"] >= 1
    swept = await repo.get_reservation(reservation.reservation_id)
    assert swept.state == ReservationState.UNCERTAIN
    # Still counted: expiry triggers investigation, not release.
    snapshot = await service.get_user_snapshot("sweep-user", FixedSettings(1000))
    assert snapshot.reserved == 100


@pytest.mark.asyncio
async def test_outstanding_exposure_guard_blocks_hoarding(env):
    _manager, _repo, service = env
    limit = 1000
    # ratio 0.5 → holds above 500 refused even though the day has room.
    for i in range(6):
        try:
            await service.reserve(
                user_id="hoard-user", estimated_units=100,
                call_id=f"hold-{i}" if i else None,
                settings_repo=FixedSettings(limit),
            )
        except BudgetError as exc:
            assert exc.code == BudgetErrorCode.BUDGET_EXHAUSTED
            assert exc.details.get("scope") == "outstanding_holds"
            return
    pytest.fail("exposure guard never triggered")


# ── normalizer (§2.12) ────────────────────────────────────────────────


def test_gemini_normalizer_keeps_unknown_fields_unknown():
    usage = normalize_gemini_usage(type("U", (), {"prompt_token_count": 10, "candidates_token_count": None, "total_token_count": None})())
    assert usage.input_tokens == 10
    assert usage.output_tokens is None  # missing stays unknown, not 0
    assert usage.measurement_source == "provider_reported"
    assert usage.policy_units() == 10


def test_openai_normalizer_does_not_double_count_reasoning():
    usage = normalize_openai_usage(
        {"prompt_tokens": 50, "completion_tokens": 40, "total_tokens": 90,
         "completion_tokens_details": {"reasoning_tokens": 15}},
        provider="groq",
    )
    assert usage.reasoning_tokens == 15  # retained for reconciliation
    assert usage.policy_units() == 90    # NOT 90 + 15


def test_estimate_units_is_estimate_only():
    assert estimate_units_from_text("a" * 401) == 101
    usage = NormalizedUsage(provider="x", measurement_source="unknown")
    assert usage.measurement_source != "provider_reported"


# ── §2.18: no dual-write double counting ─────────────────────────────


@pytest.mark.asyncio
async def test_legacy_recording_and_settlement_do_not_double_count(env):
    """A metered call settles through its reservation. If a legacy path ALSO
    recorded the same call via add_usage, the user would be charged twice —
    the migration rule is exactly one authoritative write per call."""
    _manager, repo, service = env
    async with service.metered_call(
        user_id="dual-user", estimated_input_units=100, output_reserve_units=0,
        settings_repo=FixedSettings(1000),
    ) as call:
        call.capture_usage(NormalizedUsage(
            provider="gemini", reported_total_tokens=120,
            measurement_source="provider_reported",
        ))
    # Legacy write for an UNRELATED call is fine; the SAME call must not be
    # written twice. (The metered path never calls add_usage.)
    await repo.add_usage("dual-user", TODAY, 30)
    snapshot = await service.get_user_snapshot("dual-user", FixedSettings(1000))
    assert snapshot.consumed == 150  # 120 settled + 30 legacy, not 240


# ── metrics (§2.19) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accounting_metrics_are_recorded_without_user_identifiers(env):
    _manager, _repo, service = env
    async with service.metered_call(
        user_id="metric-user", estimated_input_units=10, output_reserve_units=0,
        settings_repo=FixedSettings(1000),
    ) as call:
        call.capture_usage(NormalizedUsage(
            provider="gemini", reported_total_tokens=10,
            measurement_source="provider_reported",
        ))
    metrics = budget_metrics()
    blob = str(metrics)
    assert "budget_admissions" in blob and "budget_settlements" in blob
    assert "metric-user" not in blob, "metrics must not embed user identifiers"
