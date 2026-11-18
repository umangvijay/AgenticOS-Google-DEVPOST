"""Phase 30 / 30.32 — the WORKED LEDGER, end to end.

A provider receipt of 1,000 input including 300 cached plus 200 output
including 40 reasoning settles 1,200 units when the provider defines
those subsets. Repeated receipt delivery changes nothing. A cancelled
dispatched call without final usage remains visible pending
reconciliation. Money = measured quantities × the versioned tariff.
"""
import json
import os

import pytest
import pytest_asyncio

os.environ.setdefault("SECRETS_MASTER_KEY", "phase30-test-master-key-0123456789abcdef")

from backend.models.usage import (  # noqa: E402
    SettlementReceipt, MeasurementSource, BudgetScope, ReservationRequest,
    BudgetEventType,
)
from backend.repositories.sqlite.database import DatabaseManager  # noqa: E402
from backend.repositories.usage_repository import (  # noqa: E402
    SQLiteUsageRepository, BucketLimit,
)
from backend.services.budget_service import BudgetService  # noqa: E402
from backend.services.pricing import compute_cost  # noqa: E402
from backend.services.usage_normalizer import (  # noqa: E402
    NormalizedUsage, normalize_openai_usage,
)
from backend.services.provider_rate_limit import (  # noqa: E402
    record_observation, latest_observation, clear,
)

ALLOWANCE = 10_000
BUCKET = [BucketLimit(BudgetScope.USER, "ledger-user", ALLOWANCE)]


@pytest_asyncio.fixture
async def env(tmp_path):
    manager = DatabaseManager(str(tmp_path / "worked-ledger.db"))
    await manager.initialize()
    repo = SQLiteUsageRepository(manager)
    service = BudgetService(repo)
    yield manager, repo, service
    clear()
    await manager.close()


async def _snapshot(service):
    # The fixture policy limit is 10,000/day (the BucketLimit above); the
    # service-level path resolves the process default, so read the bucket
    # directly for the worked-ledger arithmetic.
    from backend.services.budget_service import utc_period_day
    return await service.repo.get_budget_snapshot(
        BudgetScope.USER, "ledger-user", utc_period_day(), ALLOWANCE)


async def _open_settled_usage(repo, service):
    """Opening state: settled usage of 2,000, no holds."""
    reservation = await repo.reserve(
        ReservationRequest(user_id="ledger-user", units=2000), BUCKET)
    await repo.mark_dispatching(reservation.reservation_id)
    await repo.settle(reservation.reservation_id, SettlementReceipt(
        reservation_id=reservation.reservation_id, units=2000,
        measurement_source=MeasurementSource.PROVIDER_REPORTED,
        provider_request_id="req-opening"))


@pytest.mark.asyncio
async def test_worked_ledger_end_to_end(env):
    manager, repo, service = env
    await _open_settled_usage(repo, service)

    # Reserve 1,500 for attempt A → available 6,500.
    a = await service.reserve(user_id="ledger-user", estimated_units=1500,
                              provider="gemini", model="gemini-3.8-flash",
                              purpose="generation", run_id="run-a",
                              request_key="attempt-a")
    snap = await _snapshot(service)
    assert snap.available == 6500
    await service.mark_dispatching(a.reservation_id)

    # Provider reports 1,000 input incl. 300 cached + 200 output incl.
    # 40 reasoning (total 1,200) → settle 1,200, hold remainder released.
    receipt_usage = normalize_openai_usage({
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "total_tokens": 1200,
        "prompt_tokens_details": {"cached_tokens": 300},
        "completion_tokens_details": {"reasoning_tokens": 40},
    }, provider="gemini", resolved_model="gemini-3.8-flash")
    assert receipt_usage.cached_input_tokens == 300
    assert receipt_usage.reasoning_tokens == 40
    assert receipt_usage.policy_units() == 1200  # subsets, never added twice
    await service.settle(a.reservation_id, SettlementReceipt(
        reservation_id=a.reservation_id, units=1200,
        measurement_source=MeasurementSource.PROVIDER_REPORTED,
        provider_request_id="req-a", resolved_model="gemini-3.8-flash",
        input_tokens=1000, output_tokens=200, cached_input_tokens=300,
        reasoning_tokens=40, reported_total_tokens=1200,
        pricing_version="tariff-2026-11", cost_amount=0.00315,
        cost_currency="USD"))
    snap = await _snapshot(service)
    assert snap.consumed == 3200 and snap.reserved == 0
    assert snap.available == 6800

    # Re-delivery of the SAME receipt changes neither figure (dedup on
    # the receipt digest — identical fields ⇒ identical digest).
    await service.settle(a.reservation_id, SettlementReceipt(
        reservation_id=a.reservation_id, units=1200,
        measurement_source=MeasurementSource.PROVIDER_REPORTED,
        provider_request_id="req-a", resolved_model="gemini-3.8-flash",
        input_tokens=1000, output_tokens=200, cached_input_tokens=300,
        reasoning_tokens=40, reported_total_tokens=1200,
        pricing_version="tariff-2026-11", cost_amount=0.00315,
        cost_currency="USD"))
    snap = await _snapshot(service)
    assert snap.consumed == 3200 and snap.available == 6800

    # Money: 700×$2/1M + 300×$0.50/1M + 200×$8/1M = $0.00315 under the
    # illustrative tariff; the 40 reasoning tokens are inside output.
    price = compute_cost(receipt_usage, model="gemini-3.8-flash")
    assert price.amount == pytest.approx(0.00315, abs=1e-9)
    assert price.currency == "USD"
    assert price.quantities == {"input_uncached": 700, "input_cached": 300,
                                "output": 200}

    # Reserve 800 for attempt B; cancellation after dispatch but before
    # final usage → 3,200 settled, 800 uncertain, 6,000 available.
    b = await service.reserve(user_id="ledger-user", estimated_units=800,
                              provider="gemini", request_key="attempt-b")
    await service.mark_dispatching(b.reservation_id)
    await service.mark_uncertain(b.reservation_id, "cancelled after dispatch")
    snap = await _snapshot(service)
    assert snap.consumed == 3200
    assert snap.reserved == 800 and snap.uncertain_reserved == 800
    assert snap.available == 6000

    # Separately admit and settle an authorized fallback C for 350.
    c = await service.reserve(user_id="ledger-user", estimated_units=350,
                              provider="gemini", request_key="attempt-c")
    await service.mark_dispatching(c.reservation_id)
    await service.settle(c.reservation_id, SettlementReceipt(
        reservation_id=c.reservation_id, units=350,
        measurement_source=MeasurementSource.PROVIDER_REPORTED,
        provider_request_id="req-c"))
    snap = await _snapshot(service)
    assert snap.consumed == 3550 and snap.reserved == 800
    assert snap.available == 5650

    # A late B receipt of 500 replaces the hold: settled 4,050, held 0,
    # available 5,950.
    await service.settle(b.reservation_id, SettlementReceipt(
        reservation_id=b.reservation_id, units=500,
        measurement_source=MeasurementSource.PROVIDER_REPORTED,
        provider_request_id="req-b-late"))
    snap = await _snapshot(service)
    assert snap.consumed == 4050
    assert snap.reserved == 0 and snap.uncertain_reserved == 0
    assert snap.available == 5950


@pytest.mark.asyncio
async def test_worked_ledger_edge_receipts(env):
    manager, repo, service = env
    await _open_settled_usage(repo, service)

    # A provider that reports a TOTAL only: total settles as the unit.
    t = await service.reserve(user_id="ledger-user", estimated_units=900,
                              request_key="total-only")
    await service.mark_dispatching(t.reservation_id)
    await service.settle(t.reservation_id, SettlementReceipt(
        reservation_id=t.reservation_id, units=840,
        measurement_source=MeasurementSource.PROVIDER_REPORTED,
        provider_request_id="req-total"))
    snap = await _snapshot(service)
    assert snap.consumed == 2840

    # Cancellation BEFORE dispatch: the hold is released with evidence,
    # never counted (proven unused).
    d = await service.reserve(user_id="ledger-user", estimated_units=400,
                              request_key="pre-dispatch-cancel")
    await service.release_unused(d.reservation_id, evidence={
        "phase": "pre_dispatch", "error": "CancelledError"})
    snap = await _snapshot(service)
    assert snap.consumed == 2840 and snap.reserved == 0

    # A genuine measured zero settles zero — distinct from unknown.
    z = await service.reserve(user_id="ledger-user", estimated_units=100,
                              request_key="measured-zero")
    await service.mark_dispatching(z.reservation_id)
    await service.settle(z.reservation_id, SettlementReceipt(
        reservation_id=z.reservation_id, units=0,
        measurement_source=MeasurementSource.PROVIDER_REPORTED,
        provider_request_id="req-zero"))
    snap = await _snapshot(service)
    assert snap.consumed == 2840

    # A receipt ABOVE the reservation settles the overrun (never clipped).
    o = await service.reserve(user_id="ledger-user", estimated_units=300,
                              request_key="overrun")
    await service.mark_dispatching(o.reservation_id)
    await service.settle(o.reservation_id, SettlementReceipt(
        reservation_id=o.reservation_id, units=520,
        measurement_source=MeasurementSource.PROVIDER_REPORTED,
        provider_request_id="req-overrun"))
    async with repo._write_tx() as conn:
        row = await (await conn.execute(
            "SELECT payload_json FROM budget_events WHERE event_type=? "
            "AND reservation_id=? ORDER BY created_at DESC LIMIT 1",
            (BudgetEventType.SETTLED.value, o.reservation_id),
        )).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["overrun"] is True and payload["actual_units"] == 520
    assert (await _snapshot(service)).consumed == 3360


@pytest.mark.asyncio
async def test_provider_rate_limit_surface_and_absence(env):
    manager, repo, service = env
    # Absent data stays absent — the UI renders explicit unavailability.
    snapshot = await service.get_user_snapshot("ledger-user")
    assert snapshot.provider_rate_limit is None

    record_observation(
        "ledger-user", provider="gemini", source="provider_header",
        scope="project", limit=1000, remaining=742,
        reset_at="2026-11-14T18:00:00Z", window="1m")
    snapshot = await service.get_user_snapshot("ledger-user")
    assert snapshot.provider_rate_limit["source"] == "provider_header"
    assert snapshot.provider_rate_limit["remaining"] == 742
    assert snapshot.provider_rate_limit["reset_at"] == "2026-11-14T18:00:00Z"

    with pytest.raises(ValueError):
        record_observation("ledger-user", provider="gemini",
                           source="guessed")  # invented sources refused


@pytest.mark.asyncio
async def test_pricing_is_explicit_when_unknown(env):
    from backend.services.usage_normalizer import NormalizedUsage
    # Unknown model → explicitly unavailable, never zero.
    price = compute_cost(NormalizedUsage(provider="gemini", input_tokens=10),
                         model="unlisted-model")
    assert price.amount is None and price.unavailable_reason
    # Total-only provider semantics are declared, not invented.
    price = compute_cost(NormalizedUsage(provider="x", reported_total_tokens=500),
                         model="gemini-3.8-flash")
    assert price.amount == pytest.approx(0.001, abs=1e-9)
