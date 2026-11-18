"""Phase 5 §5.20: effect identity, dispatch boundaries, email effects
(deterministic SMTP simulators), and evidence-based reconciliation.

Tests run through the SERVICE boundary (§5.20's explicit requirement —
repository-only tests missed the C1/C2 integration defects).
"""
import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.models.effect import (
    Effect,
    EffectState,
    EffectTransitionError,
    new_effect_id,
)
from backend.repositories.effect_repository import (
    SQLiteEffectRepository,
    request_digest,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "effects.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def repo(db):
    return SQLiteEffectRepository(db)


# ── §5.2/§5.3: identity survives retries; changes are detectable ──────

def test_request_digest_detects_action_changes():
    """§5.3 acceptance test: changing a recipient, target, or amount produces
    a detectable digest change; identical semantic requests do not."""
    base = {"recipients": ["a@x.io", "b@x.io"], "subject": "Hi", "amount": 5}
    d1 = request_digest("u1", "email.send", base)
    assert d1 == request_digest("u1", "email.send", dict(reversed(list(base.items()))))
    assert d1 != request_digest("u1", "email.send", {**base, "recipients": ["a@x.io"]})
    assert d1 != request_digest("u1", "email.send", {**base, "amount": 6})
    assert d1 != request_digest("u2", "email.send", base)      # tenant-scoped
    assert d1 != request_digest("u1", "http.request", base)    # operation-scoped


@pytest.mark.asyncio
async def test_transport_retry_preserves_effect_identity(repo):
    """§5.2 rule 1: the same semantic request returns the SAME effect."""
    effect, created = await repo.prepare_effect(Effect(
        effect_id=new_effect_id(), operation_name="email.send",
        owner_user_id="u1", request_digest=request_digest("u1", "email.send",
                                                          {"r": ["a@x.io"]}),
        target="a@x.io"))
    assert created is True
    retry, created2 = await repo.prepare_effect(Effect(
        effect_id=new_effect_id(), operation_name="email.send",
        owner_user_id="u1", request_digest=request_digest("u1", "email.send",
                                                          {"r": ["a@x.io"]}),
        target="a@x.io"))
    assert created2 is False
    assert retry.effect_id == effect.effect_id


# ── §5.5/§5.6/§5.8: email through the effect boundary ─────────────────

def _smtp_simulator(behavior: str, refused: dict | None = None):
    """§5.20: a deterministic local SMTP simulator."""
    calls = []

    def send(message):
        calls.append(message)
        if behavior == "disconnect":
            raise ConnectionError("connection lost after DATA")
        if behavior == "refuse_all":
            return {r: (550, b"user unknown") for r in message.get("To").split(", ")}
        if behavior == "refuse_some":
            to_list = message.get("To").split(", ")
            return {to_list[-1]: (550, b"mailbox unavailable")}
        return {}   # all accepted

    send.calls = calls
    return send


@pytest.mark.asyncio
async def test_email_all_accepted_is_confirmed_applied(repo):
    """§5.8: server acceptance is CONFIRMED_APPLIED for the SUBMISSION —
    with the receipt recording that delivery is not confirmed."""
    from backend.services.effect_service import send_email_effect
    smtp = _smtp_simulator("accept_all")
    result = await _send_via(repo, smtp, ["a@x.io"], run_id="run-1",
                             task_id="task-1")
    assert result.outcome.value == "confirmed_applied"
    assert "Accepted by your mail server" in result.safe_user_message
    assert "delivery not yet confirmed" in result.safe_user_message
    assert result.retry_disposition == "none"
    receipts = await repo.receipts_for(result.effect_id)
    assert receipts and receipts[0]["provider_status"] == "smtp_accepted"
    assert receipts[0]["provider_operation_id"]      # stable Message-ID
    # Effect identity binds run/task (§5.3).
    effect = await repo.get_effect(result.effect_id)
    assert effect.run_id == "run-1" and effect.task_id == "task-1"


async def _send_via(repo, smtp, recipients, **kw):
    from backend.services import effect_service
    user_id = kw.pop("user_id", "u1")
    semantic = {"recipients": sorted(recipients),
                "subject": kw.get("subject", "Hi"),
                "body_sha256": "x", "html": False, "from": "u1@x.io"}
    digest = request_digest(user_id, "email.send", semantic)
    dispatch = effect_service.partial_send_email_dispatch(
        {"host": "smtp.test", "port": 587, "username": "u", "password": "p",
         "from_email": "u1@x.io"},
        recipients, kw.get("subject", "Hi"), kw.get("body", "Body"), False,
        "u1@x.io", None, smtp)
    classify = effect_service.classify_smtp_outcome(recipients)
    return await effect_service.dispatch_effect(
        effect_repo=repo, operation_name="email.send", owner_user_id=user_id,
        semantic_args=semantic, request_digest=digest, provider="smtp",
        dispatch=dispatch, classify=classify,
        target=", ".join(sorted(recipients)), external_account_id="u1@x.io",
        run_id=kw.get("run_id"), task_id=kw.get("task_id"))


@pytest.mark.asyncio
async def test_email_partial_acceptance_preserved(repo):
    """§5.8/§5.15: SMTP accepting some recipients is PARTIAL — accepted
    recipients are preserved; refused ones are named and retryable."""
    smtp = _smtp_simulator("refuse_some")
    result = await _send_via(repo, smtp, ["ok@x.io", "bad@x.io"])
    assert result.outcome.value == "partially_applied"
    detail = result.receipt.detail
    assert detail["accepted"] == ["ok@x.io"]
    assert detail["refused"] == ["bad@x.io"]
    assert result.retry_disposition == "retry_items"   # explicit, attributable
    effect = await repo.get_effect(result.effect_id)
    assert effect.state is EffectState.PARTIALLY_APPLIED


@pytest.mark.asyncio
async def test_email_disconnect_preserves_unknown_no_autosend(repo):
    """§5.6/§5.16/§5.20: SMTP times out after submission → OUTCOME_UNKNOWN,
    reconciliation required, NO automatic resend (plain SMTP has no replay
    guarantee)."""
    smtp = _smtp_simulator("disconnect")
    result = await _send_via(repo, smtp, ["a@x.io"])
    assert result.outcome.value == "outcome_unknown"
    assert result.retry_disposition == "none"
    assert result.reconciliation_required is True
    effect = await repo.get_effect(result.effect_id)
    assert effect.state is EffectState.OUTCOME_UNKNOWN
    assert len(smtp.calls) == 1      # exactly one send attempt, no retry


@pytest.mark.asyncio
async def test_email_duplicate_submission_dedups_without_resending(repo):
    """§5.2/§5.20: the same semantic email submitted twice while the first
    is unsettled does NOT send again — the existing effect is returned."""
    smtp = _smtp_simulator("disconnect")
    r1 = await _send_via(repo, smtp, ["a@x.io"])
    r2 = await _send_via(repo, smtp, ["a@x.io"])
    assert len(smtp.calls) == 1      # the duplicate never reached the wire
    assert r2.effect_id == r1.effect_id
    assert r2.reconciliation_required is True


# ── §5.10/§5.11/§5.12: evidence-based reconciliation ──────────────────

@pytest.mark.asyncio
async def test_reconciliation_evidence_resolves_unknown(repo):
    """§5.10: an operation-specific evidence strategy confirms the outcome;
    absence of a strategy preserves UNKNOWN (§5.12)."""
    from backend.services import reconciliation_service as rs
    from backend.models.effect import new_effect_id
    effect_id = new_effect_id()
    await repo.prepare_effect(Effect(
        effect_id=effect_id, operation_name="email.send", owner_user_id="u1",
        request_digest="d-rcn", target="a@x.io"))
    await repo.transition(effect_id, EffectState.DISPATCHING, actor="t")
    await repo.transition(effect_id, EffectState.OUTCOME_UNKNOWN, actor="t")

    # No strategy yet: the unknown outcome is PRESERVED.
    record = await rs.reconcile_effect(repo, effect_id)
    assert record["resolved"] is False
    assert (await repo.get_effect(effect_id)).state is EffectState.OUTCOME_UNKNOWN

    # A provider-specific strategy finds the message on the server.
    async def strategy(effect):
        return ({"smtp_search": "found", "message_id_match": True},
                EffectState.CONFIRMED_APPLIED)
    rs.register_evidence_strategy("email.send", strategy)
    try:
        record = await rs.reconcile_effect(repo, effect_id)
        assert record["resolved"] is True
        assert (await repo.get_effect(effect_id)).state is EffectState.CONFIRMED_APPLIED
    finally:
        rs._STRATEGIES.pop("email.send", None)


@pytest.mark.asyncio
async def test_empty_search_is_not_proof_of_non_execution(repo):
    """§5.10: absence is not automatically proof — an empty search result
    with no supporting evidence policy leaves the effect UNKNOWN."""
    from backend.services import reconciliation_service as rs
    from backend.models.effect import new_effect_id
    effect_id = new_effect_id()
    await repo.prepare_effect(Effect(
        effect_id=effect_id, operation_name="email.send", owner_user_id="u1",
        request_digest="d-empty", target="a@x.io"))
    await repo.transition(effect_id, EffectState.DISPATCHING, actor="t")
    await repo.transition(effect_id, EffectState.OUTCOME_UNKNOWN, actor="t")

    async def empty_search(effect):
        return ({"smtp_search": "empty"}, None)   # cannot confirm non-application
    rs.register_evidence_strategy("email.send", empty_search)
    try:
        record = await rs.reconcile_effect(repo, effect_id)
        assert record["resolved"] is False
        assert (await repo.get_effect(effect_id)).state is EffectState.OUTCOME_UNKNOWN
    finally:
        rs._STRATEGIES.pop("email.send", None)


@pytest.mark.asyncio
async def test_concurrent_resolutions_one_winner_one_conflict(repo):
    """§5.11/§5.20: two resolvers racing an uncertain effect — exactly one
    conditional resolution wins; the other receives a conflict."""
    from backend.services import reconciliation_service as rs
    from backend.models.effect import new_effect_id
    effect_id = new_effect_id()
    effect, _ = await repo.prepare_effect(Effect(
        effect_id=effect_id, operation_name="email.send", owner_user_id="u1",
        request_digest="d-race", target="a@x.io"))
    await repo.transition(effect_id, EffectState.DISPATCHING, actor="t")
    await repo.transition(effect_id, EffectState.OUTCOME_UNKNOWN, actor="t")

    r1 = await rs.resolve_effect(
        repo, effect_id, expected_version=effect.state_version + 2,
        resolution=EffectState.CONFIRMED_APPLIED,
        evidence_references=["smtp:search:found"], actor="op-1",
        reason="evidence found")
    r2 = await rs.resolve_effect(
        repo, effect_id, expected_version=effect.state_version + 2,
        resolution=EffectState.CONFIRMED_APPLIED,
        evidence_references=["smtp:search:found"], actor="op-2",
        reason="evidence found")
    winners = [r for r in (r1, r2) if r.get("resolved")]
    conflicts = [r for r in (r1, r2) if r.get("error") == "CONFLICT"]
    assert len(winners) == 1 and len(conflicts) == 1


@pytest.mark.asyncio
async def test_operator_guess_cannot_manufacture_confirmed_not_applied(repo):
    """§5.12: an operator decision WITHOUT evidence can close unresolved but
    can never fabricate CONFIRMED_NOT_APPLIED (unsafe-replay rule)."""
    from backend.services import reconciliation_service as rs
    from backend.models.effect import new_effect_id
    effect_id = new_effect_id()
    effect, _ = await repo.prepare_effect(Effect(
        effect_id=effect_id, operation_name="email.send", owner_user_id="u1",
        request_digest="d-guess", target="a@x.io"))
    await repo.transition(effect_id, EffectState.DISPATCHING, actor="t")
    await repo.transition(effect_id, EffectState.OUTCOME_UNKNOWN, actor="t")

    refused = await rs.resolve_effect(
        repo, effect_id, expected_version=effect.state_version + 2,
        resolution=EffectState.CONFIRMED_NOT_APPLIED,
        evidence_references=[],          # no evidence
        actor="operator", reason="probably did not send")
    assert refused["resolved"] is False

    closed = await rs.resolve_effect(
        repo, effect_id, expected_version=effect.state_version + 2,
        resolution=EffectState.CLOSED_UNRESOLVED,
        evidence_references=[], actor="operator", reason="stop investigating")
    assert closed["resolved"] is True
    assert (await repo.get_effect(effect_id)).state is EffectState.CLOSED_UNRESOLVED
