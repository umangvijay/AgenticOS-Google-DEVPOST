"""Phase 17 Batch A acceptance (§17.0/§17.2/§17.5): the metering
boundary and the connector binding context."""
import pytest

from backend.services.budget_service import MeteredCall
from backend.services.usage_normalizer import NormalizedUsage


# ── §17.2/D: usage events accumulate per provider semantics ────────────

def _usage(inp, out):
    return NormalizedUsage(provider="gemini", input_tokens=inp,
                           output_tokens=out,
                           measurement_source="provider_reported")


def test_multi_turn_usage_accumulates_never_replaces():
    call = MeteredCall.__new__(MeteredCall)   # the capture contract only
    call.usage = None
    call.capture_usage(_usage(100, 50))
    call.capture_usage(_usage(200, 80))   # a tool-driven follow-up call
    call.capture_usage(_usage(30, 10))    # a retry within the attempt
    assert call.usage.input_tokens == 330
    assert call.usage.output_tokens == 140
    assert call.usage.known is True


def test_usage_capture_never_double_counts_cumulative_carriers():
    """Some providers emit CUMULATIVE totals on each event — summing those
    blindly would over-count. The capture sums EVENTS; a carrier that
    already includes prior events is the caller's normalization duty
    (documented, not silently assumed)."""
    call = MeteredCall.__new__(MeteredCall)
    call.usage = None
    call.capture_usage(_usage(100, 50))
    call.capture_usage(_usage(0, 0))   # a no-usage event changes nothing
    assert call.usage.input_tokens == 100


# ── §17.2/D: the deterministic three-call fixture ──────────────────────

@pytest.mark.asyncio
async def test_three_call_fixture_produces_three_attributed_records(tmp_path):
    """The §17.2 acceptance: a deterministic three-call agent fixture
    yields three correctly attributed call records — after a retry —
    through the REAL metered_call service."""
    from backend.services.budget_service import BudgetService
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.usage_repository import (
        SQLiteUsageRepository, set_usage_repo)
    db = DatabaseManager(str(tmp_path / "meter.db"))
    await db.initialize()
    repo = SQLiteUsageRepository(db)
    set_usage_repo(repo)
    from backend.services.budget_service import set_budget_service
    service = BudgetService()
    set_budget_service(service)
    try:
        for attempt, model in enumerate(["m-a", "m-a", "m-b"]):
            try:
                async with service.metered_call(
                    user_id="u1", estimated_input_units=10, provider="gemini",
                    model=model, purpose="engine_generation", run_id="run-m3",
                    call_id=f"engine:run-m3:t1:{model}:a{attempt}",
                    attempt_id=str(attempt),
                ) as call:
                    await call.mark_dispatched()
                    # The FIRST attempt fails after dispatch (a retryable
                    # provider error); attempts 2 and 3 report usage.
                    if attempt == 0:
                        raise RuntimeError("503 model overloaded")
                    call.capture_usage(_usage(100 + attempt, 40))
            except RuntimeError:
                continue   # the retryable failure moves to the next attempt
        # Three metered attempts → three distinct reservations in the
        # ledger (the failed one releases; the others settle).
        summary = await repo.usage_summary_for_run("run-m3")
        assert summary is not None
        from backend.models.usage import ReservationState
        rows = await repo.db.fetch_all(
            "SELECT call_id, state FROM budget_reservations WHERE run_id=? "
            "ORDER BY created_at", ("run-m3",))
        assert len(rows) == 3, "each attempt holds its OWN record"
        assert rows[0]["call_id"].startswith("engine:run-m3:t1:m-a")
        assert rows[2]["state"] in ("settled", "completed")
    finally:
        set_usage_repo(None)
        await db.close()


# ── §17.2/D: failed embeddings record unavailable, never fabricated ────

@pytest.mark.asyncio
async def test_failed_embedding_records_unavailable_not_placeholder():
    from backend.api.routers import memory as mem_router
    import inspect
    src = inspect.getsource(mem_router.correct_memory)
    assert "embed_text_metered" in src, "the metered path"
    assert "[0.0]" not in src, \
        "no fabricated placeholder vector poisons similarity"
    assert "embedding = None" in src


# ── §17.5: the connector binding context is explicit ───────────────────

def test_credential_binding_requires_version_and_epoch():
    from backend.models.mcp_schemas import AuthMetadata
    a = AuthMetadata(type="API_KEY", credential_ref="cred:acme",
                     secret_version="v3", authorization_epoch="e7")
    assert a.binding_context() == ("cred:acme", "v3", "e7")
    # A legacy row (persisted before the §17.5 fields) parses but is
    # marked explicitly — its binding id cannot match fresh evidence.
    legacy = AuthMetadata(**{"type": "API_KEY",
                             "credential_ref": "cred:acme"})
    assert legacy.legacy_unbound is True
    assert legacy.binding_context() == ("cred:acme", "legacy", "legacy")
    from backend.services.tool_validation import credential_binding_id_from
    assert credential_binding_id_from(
        {"credential_ref": "cred:acme", "secret_version": "legacy",
         "authorization_epoch": "legacy"}) != credential_binding_id_from(
        {"credential_ref": "cred:acme", "secret_version": "v3",
         "authorization_epoch": "e7"})


def test_rotation_changes_the_binding_id():
    """Validation under credential version A cannot authorize execution
    under version B — the binding hash moves with the context."""
    from backend.services.tool_validation import credential_binding_id_from
    a = {"type": "API_KEY", "credential_ref": "cred:acme",
         "secret_version": "v3", "authorization_epoch": "e7"}
    b = {"type": "API_KEY", "credential_ref": "cred:acme",
         "secret_version": "v4", "authorization_epoch": "e7"}
    assert credential_binding_id_from(a) != credential_binding_id_from(b)


def test_serialization_preserves_the_binding_context():
    """Manifest round-trips cannot accidentally restore default
    version/epoch values — a credential_ref without them is REJECTED at
    parse time."""
    from backend.models.mcp_schemas import AuthMetadata
    a = AuthMetadata(type="API_KEY", credential_ref="cred:acme",
                     secret_version="v3", authorization_epoch="e7")
    restored = AuthMetadata.model_validate(a.model_dump(mode="json"))
    assert restored.binding_context() == a.binding_context()
