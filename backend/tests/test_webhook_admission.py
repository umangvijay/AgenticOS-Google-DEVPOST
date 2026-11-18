"""Phase 3 §3.15/§3.21: webhook ingress command admission.

A repeated webhook delivery must not create another logical workflow, and a
flood of distinct deliveries cannot bypass the OWNER's bounded queue —
webhook-triggered runs consume the owner's pending slots like any other
submission.
"""
import hashlib
import hmac
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend.security.webhook_signature import verify_signature

SECRET = "whsec-test-secret"


def _signed_body(body: bytes = b'{"hello": "world"}'):
    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + body,
                               hashlib.sha256).hexdigest()
    return body, ts, sig


def _make_request(factory, body: bytes, ts: str, sig: str,
                  workflow_engine=None):
    async def body_stream():
        yield body

    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            factory=factory,
            workflow_engine=workflow_engine or object())),
        headers={"content-length": str(len(body)),
                 "x-agentos-timestamp": ts, "x-agentos-signature": sig},
        client=SimpleNamespace(host="203.0.113.50"),
        stream=body_stream,
    )


@pytest_asyncio.fixture
async def command_store(tmp_path):
    """§4.16: acceptance needs the durable command repository — a real
    SQLite store, installed for the route and cleared after."""
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.command_repository import SQLiteCommandRepository
    from backend.services.command_service import set_command_repo
    db = DatabaseManager(str(tmp_path / "cmd.db"))
    await db.initialize()
    set_command_repo(SQLiteCommandRepository(db))
    yield db
    set_command_repo(None)
    await db.close()


@pytest_asyncio.fixture
async def admission_on_command_store(command_store):
    """The queue-admission repository shares the command store so the
    acceptance transaction counts the same pending_commands rows."""
    from backend.repositories.admission_repository import (
        SQLiteCommandAdmissionRepository, set_command_admission_repo,
    )
    repo = SQLiteCommandAdmissionRepository(command_store)
    set_command_admission_repo(repo)
    yield repo
    set_command_admission_repo(None)


@pytest.fixture
def factory_stub():
    f = SimpleNamespace()
    f.schedule_repo = SimpleNamespace(get_schedule=AsyncMock(return_value={
        "schedule_id": "s1", "user_id": "owner-1", "goal": "g", "status": "ACTIVE"}))
    f.secrets_repo = SimpleNamespace(get_secret=AsyncMock(return_value="enc"))
    # Stateful replay ledger: atomic_insert(key) returns True once per key,
    # False on redelivery — the route's REAL replay gate.
    seen_keys = set()

    async def atomic_insert(key, *_a, **_k):
        if key in seen_keys:
            return False
        seen_keys.add(key)
        return True

    f.idempotency_repo = SimpleNamespace(
        atomic_insert=atomic_insert,
        commit_success=AsyncMock(return_value=None))
    f.workflow_repo = SimpleNamespace(save_run=AsyncMock(return_value=None))
    return f


@pytest.fixture
def signed_env(monkeypatch):
    from backend.security.secrets_vault import secrets_vault
    monkeypatch.setattr(secrets_vault, "decrypt", lambda _enc: SECRET)
    # The webhook route imports these INSIDE the handler — patch the
    # source module, not the router namespace.
    monkeypatch.setattr("backend.security.rate_limiter.check_rate_limit",
                        lambda *a, **k: None)
    monkeypatch.setattr("backend.security.rate_limiter.check_shared_rate_limit",
                        AsyncMock(return_value={"shared": True}))
    monkeypatch.setattr("backend.security.rate_limiter.get_client_identifier",
                        lambda _r: "ip:test")
    return None


@pytest.mark.asyncio
async def test_webhook_owner_at_pending_cap_gets_429(factory_stub, signed_env,
                                                     admission_on_command_store,
                                                     monkeypatch):
    """§3.21: distinct webhook deliveries consume the OWNER's queue slots —
    a flood is refused with QUEUE_FULL, not silently accepted."""
    from backend.api.routers import webhooks
    from backend.config.settings import settings

    monkeypatch.setattr(settings, "TENANT_MAX_PENDING_COMMANDS", 2)
    try:
        # Fill the owner's queue directly (2 accepted, unsettled commands)
        # in the SAME store the acceptance transaction counts.
        for i in range(2):
            r = await _admit_direct(i)
            assert r.accepted

        body, ts, sig = _signed_body()
        engine = SimpleNamespace(evaluate_dag=AsyncMock(return_value=None))
        request = _make_request(factory_stub, body, ts, sig,
                                workflow_engine=engine)
        assert verify_signature(SECRET, ts, sig, body)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await webhooks.trigger_webhook("s1", request)
        assert exc.value.status_code == 429
        assert exc.value.detail["code"] == "QUEUE_FULL"
    finally:
        pass


@pytest.mark.asyncio
async def test_webhook_replay_of_admitted_delivery_is_409(factory_stub,
                                                          signed_env,
                                                          command_store,
                                                          monkeypatch):
    """§3.15: a repeated delivery of the SAME signed payload is a duplicate
    command — 409 with the original command id, never a second run."""
    from backend.api.routers import webhooks
    from backend.repositories.admission_repository import (
        SQLiteCommandAdmissionRepository, set_command_admission_repo,
    )
    from backend.repositories.sqlite.database import DatabaseManager
    import tempfile, os

    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "wh2.db"))
    await db.initialize()
    set_command_admission_repo(SQLiteCommandAdmissionRepository(db))
    try:
        body, ts, sig = _signed_body()

        async def success_run(_factory, run):
            return None

        from unittest.mock import AsyncMock as _AM
        factory_stub.workflow_repo = SimpleNamespace(
            save_run=_AM(return_value=None))
        engine = SimpleNamespace(evaluate_dag=AsyncMock(return_value=None))
        factory_stub.workflow_engine = engine
        engine = SimpleNamespace(evaluate_dag=AsyncMock(return_value=None))
        request = _make_request(factory_stub, body, ts, sig,
                                workflow_engine=engine)

        first = await webhooks.trigger_webhook("s1", request)
        assert first["run_id"]

        # Redelivery: same timestamp+signature → same replay key → the
        # idempotency ledger refuses BEFORE command admission.
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await webhooks.trigger_webhook("s1", request)
        assert exc.value.status_code == 409
    finally:
        set_command_admission_repo(None)
        await db.close()


async def _admit_direct(i):
    from backend.services import admission_service
    return await admission_service.admit_command(
        f"pre-{i}", "owner-1", None, "workflow", max_wait_seconds=3600)
