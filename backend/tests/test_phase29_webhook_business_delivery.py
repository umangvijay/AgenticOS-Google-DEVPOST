"""Phase 29 revision — webhook business delivery identity (§29.19).

A tenant/source-scoped business delivery ID makes the sender's retry
identity explicit: the same event retried with a FRESH signature cannot
create a second logical run, and one ID reused with a DIFFERENT body is
a conflict refusal. The recorded digest makes the distinction durable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend.security.webhook_signature import verify_signature

SECRET = "whsec-test-secret"


_SIGNED_SEQ = [0]


def _signed_body(body: bytes):
    """A distinct in-tolerance timestamp per call — a real retry carries a
    new timestamp (and therefore a fresh signature), while staying within
    the verifier's 300 s window."""
    _SIGNED_SEQ[0] += 7
    ts = str(int(time.time()) - _SIGNED_SEQ[0])
    sig = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + body,
                               hashlib.sha256).hexdigest()
    return body, ts, sig


def _make_request(factory, body: bytes, ts: str, sig: str,
                  delivery_id: str | None = None, workflow_engine=None):
    headers = {"content-length": str(len(body)),
               "x-agentos-timestamp": ts, "x-agentos-signature": sig}
    if delivery_id:
        headers["x-agentos-delivery-id"] = delivery_id

    async def body_stream():
        yield body

    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            factory=factory,
            workflow_engine=workflow_engine or SimpleNamespace(
                evaluate_dag=AsyncMock(return_value=None)))),
        headers=headers,
        client=SimpleNamespace(host="203.0.113.50"),
        stream=body_stream,
    )


@pytest_asyncio.fixture
async def real_ledger(tmp_path):
    """A REAL SQLite idempotency ledger — get_entry must read the
    recorded digest back."""
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.idempotency_repository import (
        SQLiteIdempotencyRepository)
    from backend.repositories.command_repository import (
        SQLiteCommandRepository)
    from backend.services.command_service import set_command_repo
    db = DatabaseManager(str(tmp_path / "biz-webhook.db"))
    await db.initialize()
    ledger = SQLiteIdempotencyRepository(db)
    set_command_repo(SQLiteCommandRepository(db))
    yield ledger
    set_command_repo(None)
    await db.close()


@pytest.fixture
def factory_stub(real_ledger):
    f = SimpleNamespace()
    f.schedule_repo = SimpleNamespace(get_schedule=AsyncMock(return_value={
        "schedule_id": "s1", "user_id": "owner-1", "goal": "g",
        "status": "ACTIVE"}))
    f.secrets_repo = SimpleNamespace(get_secret=AsyncMock(return_value="enc"))
    f.idempotency_repo = real_ledger
    f.workflow_repo = SimpleNamespace(save_run=AsyncMock(return_value=None))
    return f


@pytest.fixture
def signed_env(monkeypatch):
    from backend.security.secrets_vault import secrets_vault
    monkeypatch.setattr(secrets_vault, "decrypt", lambda _enc: SECRET)
    monkeypatch.setattr("backend.security.rate_limiter.check_rate_limit",
                        lambda *a, **k: None)
    monkeypatch.setattr("backend.security.rate_limiter.check_shared_rate_limit",
                        AsyncMock(return_value={"shared": True}))
    monkeypatch.setattr("backend.security.rate_limiter.get_client_identifier",
                        lambda _r: "ip:test")
    return None


def _acceptance_stub(monkeypatch):
    """Accept every command with a fresh run id."""
    from backend.models.command import CommandReceipt, CommandState
    from backend.services import command_service
    async def _accept(**kwargs):
        return CommandReceipt(
            command_id=kwargs.get("idempotency_key", "cmd"),
            run_id=kwargs.get("run_id"), state=CommandState.ACCEPTED,
            accepted=True, deduplicated=False, state_version=1,
            status_url="/commands/x")
    monkeypatch.setattr(command_service, "accept_command", _accept)


@pytest.mark.asyncio
async def test_fresh_signature_same_delivery_id_is_one_logical_run(
        factory_stub, signed_env, monkeypatch):
    """The SAME business delivery retried with a fresh signature cannot
    create a second run — the recorded digest dedups it."""
    from backend.api.routers import webhooks
    _acceptance_stub(monkeypatch)
    body = json.dumps({"event": "change", "records": [1, 2]}).encode()
    b1, ts1, sig1 = _signed_body(body)
    first = await webhooks.trigger_webhook(
        "s1", _make_request(factory_stub, b1, ts1, sig1,
                            delivery_id="d19"))
    assert first.get("deduplicated") is not True
    run_a = first["run_id"]

    b2, ts2, sig2 = _signed_body(body)  # fresh timestamp + signature
    assert sig2 != sig1
    second = await webhooks.trigger_webhook(
        "s1", _make_request(factory_stub, b2, ts2, sig2,
                            delivery_id="d19"))
    assert second["deduplicated"] is True
    assert second["run_id"] == run_a, \
        "the same business delivery resolves to the ORIGINAL run"


@pytest.mark.asyncio
async def test_conflicting_body_under_one_delivery_id_is_refused(
        factory_stub, signed_env, monkeypatch):
    from backend.api.routers import webhooks
    _acceptance_stub(monkeypatch)
    from fastapi import HTTPException
    body_a = json.dumps({"event": "change", "records": [1]}).encode()
    body_b = json.dumps({"event": "change", "records": [2]}).encode()
    b1, ts1, sig1 = _signed_body(body_a)
    await webhooks.trigger_webhook(
        "s1", _make_request(factory_stub, b1, ts1, sig1,
                            delivery_id="d20"))
    b2, ts2, sig2 = _signed_body(body_b)
    with pytest.raises(HTTPException) as exc_info:
        await webhooks.trigger_webhook(
            "s1", _make_request(factory_stub, b2, ts2, sig2,
                                delivery_id="d20"))
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "BUSINESS_DELIVERY_CONFLICT"


@pytest.mark.asyncio
async def test_different_delivery_ids_are_independent(
        factory_stub, signed_env, monkeypatch):
    from backend.api.routers import webhooks
    _acceptance_stub(monkeypatch)
    body = json.dumps({"event": "change", "records": [3]}).encode()
    runs = []
    for delivery in ("d21", "d22"):
        b, ts, sig = _signed_body(body)
        result = await webhooks.trigger_webhook(
            "s1", _make_request(factory_stub, b, ts, sig,
                                delivery_id=delivery))
        assert result.get("deduplicated") is not True
        runs.append(result["run_id"])
    assert runs[0] != runs[1], \
        "distinct business deliveries are distinct logical runs"


@pytest.mark.asyncio
async def test_no_delivery_header_keeps_signature_dedup_baseline(
        factory_stub, signed_env, monkeypatch):
    from backend.api.routers import webhooks
    _acceptance_stub(monkeypatch)
    body = json.dumps({"event": "change"}).encode()
    b1, ts1, sig1 = _signed_body(body)
    first = await webhooks.trigger_webhook(
        "s1", _make_request(factory_stub, b1, ts1, sig1))
    b2, ts2, sig2 = _signed_body(body)
    # A fresh signature WITHOUT a delivery ID is a NEW logical run —
    # the signature replay key only dedups byte-identical deliveries.
    second = await webhooks.trigger_webhook(
        "s1", _make_request(factory_stub, b2, ts2, sig2))
    assert first["run_id"] != second["run_id"]


@pytest.mark.asyncio
async def test_delivery_id_in_payload_is_honored(
        factory_stub, signed_env, monkeypatch):
    from backend.api.routers import webhooks
    _acceptance_stub(monkeypatch)
    body = json.dumps({"event": "change", "delivery_id": "d23"}).encode()
    b1, ts1, sig1 = _signed_body(body)
    first = await webhooks.trigger_webhook(
        "s1", _make_request(factory_stub, b1, ts1, sig1))
    b2, ts2, sig2 = _signed_body(body)
    second = await webhooks.trigger_webhook(
        "s1", _make_request(factory_stub, b2, ts2, sig2))
    assert second["deduplicated"] is True
    assert second["run_id"] == first["run_id"]


def test_signature_verification_is_unchanged():
    body = b'{"a": 1}'
    ts = "1700000000"
    sig = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + body,
                               hashlib.sha256).hexdigest()
    assert verify_signature(SECRET, ts, sig, body, now=1700000010) is True
    assert verify_signature(SECRET, ts, sig, body + b" ", now=1700000010) is False
