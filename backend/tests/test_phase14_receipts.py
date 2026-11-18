"""Phase 14 finding J: comparison dataset-hash verification + activation
crash-safety.

- The comparison enqueue REFUSES case sets that do not hash to the
  dataset pinned at proposal (grading cannot drift mid-flight).
- The activation receipt is DURABLE: pending is committed before the
  pointer move, so a crash in between is reconcilable by identity —
  the retry finishes the activation instead of re-running the gates,
  and an EFFECTIVE receipt is returned as-is (never double-applied).
"""
import json

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.services.improvement_service import dataset_hash_for


@pytest_asyncio.fixture
async def db(tmp_path):
    d = DatabaseManager(str(tmp_path / "receipts.db"))
    await d.initialize()
    try:
        yield d
    finally:
        await d.close()


# ── The durable receipt store ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_receipt_is_created_pending_and_marked(db):
    from backend.services.promotion_coordinator import ActivationReceiptStore
    store = ActivationReceiptStore(db)
    first, created = await store.create_or_get(
        experiment_id="exp-r1", prompt_id="p1",
        candidate_version_id="v2", created_by="promotion:op")
    assert first["state"] == "pending" and created is True
    # create-or-get: a retry returns the SAME receipt (not a fresh one).
    again, created2 = await store.create_or_get(
        experiment_id="exp-r1", prompt_id="p1",
        candidate_version_id="v2", created_by="promotion:op")
    assert again["receipt_id"] == first["receipt_id"] and created2 is False
    await store.mark("exp-r1", "effective", active_version_id="v2")
    got = await store.get_for_experiment("exp-r1")
    assert got["state"] == "effective"
    assert got["active_version_id"] == "v2"


# ── Reconciliation: the crash windows ──────────────────────────────────

@pytest.mark.asyncio
async def test_effective_receipt_is_returned_as_is_never_double_applied(db):
    """The crash-AFTER-activation window: the retry finds an EFFECTIVE
    receipt and returns it — the pointer is not moved twice and the
    gates are not re-run."""
    from backend.services.promotion_coordinator import (
        ActivationReceiptStore, _reconcile_receipt)
    store = ActivationReceiptStore(db)
    receipt = await store.create_or_get(
        experiment_id="exp-r2", prompt_id="p1",
        candidate_version_id="v2", created_by="promotion:op")
    await store.mark("exp-r2", "effective", active_version_id="v2")

    class ActivatingStore:
        calls = 0
        async def activate(self, *a, **k):
            type(self).calls += 1
            return {"version_id": "v2"}

    out = await _reconcile_receipt(receipt, ActivatingStore(), "exp-r2",
                                   "p1", "v2", "op", store=store)
    assert out["state"] == "effective"
    assert ActivatingStore.calls == 0, "an effective receipt re-activates nothing"


@pytest.mark.asyncio
async def test_pending_receipt_is_re_driven_to_effective(db):
    """The crash-BETWEEN-promote-and-activate window: the retry finds a
    PENDING receipt, re-drives the idempotent pointer move, and marks
    the receipt effective."""
    from backend.services.promotion_coordinator import (
        ActivationReceiptStore, _reconcile_receipt)
    store = ActivationReceiptStore(db)
    receipt, _created = await store.create_or_get(
        experiment_id="exp-r3", prompt_id="p1",
        candidate_version_id="v2", created_by="promotion:op")
    assert receipt["state"] == "pending"

    class PointerStore:
        async def activate(self, prompt_id, version_id, *, activated_by):
            assert activated_by.startswith("reconcile:")
            return {"version_id": version_id}

    out = await _reconcile_receipt(receipt, PointerStore(), "exp-r3",
                                   "p1", "v2", "op", store=store)
    assert out["state"] == "effective"
    assert (await store.get_for_experiment("exp-r3"))["state"] == "effective"


@pytest.mark.asyncio
async def test_failed_activation_is_recorded_on_the_receipt(db):
    """An activation error is DURABLE evidence (state=failed, error
    text), not a lost exception."""
    from backend.services.promotion_coordinator import (
        ActivationReceiptStore, _reconcile_receipt)
    store = ActivationReceiptStore(db)
    receipt, _created = await store.create_or_get(
        experiment_id="exp-r4", prompt_id="p1",
        candidate_version_id="v-missing", created_by="promotion:op")

    class BrokenStore:
        async def activate(self, *a, **k):
            raise ValueError("Unknown prompt version v-missing")

    out = await _reconcile_receipt(receipt, BrokenStore(), "exp-r4",
                                   "p1", "v-missing", "op", store=store)
    assert out["state"] == "failed"
    assert "v-missing" in out["error"]
    durable = await store.get_for_experiment("exp-r4")
    assert durable["state"] == "failed"
    assert "v-missing" in durable["error"]


# ── The dataset-hash gate on comparison enqueue ────────────────────────

def test_dataset_hash_gate_distinguishes_pinned_from_swapped():
    """The same hash function that pins at proposal gates the enqueue —
    a swapped case set produces a different hash."""
    pinned = [{"case_id": "c1", "input": "a"}, {"case_id": "c2", "input": "b"}]
    swapped = [{"case_id": "c1", "input": "a"}, {"case_id": "c2", "input": "CHERRY"}]
    assert dataset_hash_for(pinned) != dataset_hash_for(swapped)
    assert dataset_hash_for(pinned) == dataset_hash_for(
        json.loads(json.dumps(pinned)))   # serialization-stable
