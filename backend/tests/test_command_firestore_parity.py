"""Firestore parity for the Phase 4 command lifecycle (§4.7/§4.23).

The SAME contract tests as test_command_acceptance.py against the Firestore
implementation. Runs only with a Firestore emulator configured:

    FIRESTORE_EMULATOR_HOST=localhost:8080 GOOGLE_CLOUD_PROJECT=demo \\
        pytest backend/tests/test_command_firestore_parity.py

Without an emulator they are skipped (SQLite parity is covered by
test_command_acceptance.py, which this file mirrors on purpose).
"""
import os
import uuid

import pytest
import pytest_asyncio

from types import SimpleNamespace

_RUN = uuid.uuid4().hex[:8]
_EMULATOR = os.environ.get("FIRESTORE_EMULATOR_HOST", "").strip()

pytestmark = pytest.mark.skipif(
    not _EMULATOR,
    reason="Firestore parity requires FIRESTORE_EMULATOR_HOST (e.g. localhost:8080)",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_emulator_data():
    from backend.repositories.firestore import database as fs_database
    fs_database.FirestoreDB._instance = None
    try:
        client = await fs_database.FirestoreDB.get_client()
        for name in ("commands", "outbox_events", "pending_commands",
                     "command_queue_counters"):
            async for doc in client.collection(name).stream():
                await doc.reference.delete()
    finally:
        client = fs_database.FirestoreDB._instance
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            fs_database.FirestoreDB._instance = None
    yield


@pytest_asyncio.fixture
async def repo():
    from backend.config.settings import settings as app_settings
    if not app_settings.GOOGLE_CLOUD_PROJECT:
        pytest.skip("Firestore parity requires GOOGLE_CLOUD_PROJECT (any value against the emulator)")
    from backend.repositories.firestore import database as fs_database
    from backend.repositories.firestore.command_repository import FirestoreCommandRepository
    fs_database.FirestoreDB._instance = None
    try:
        yield FirestoreCommandRepository()
    finally:
        client = fs_database.FirestoreDB._instance
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            fs_database.FirestoreDB._instance = None


def _command(**overrides):
    from backend.models.command import Command, scoped_idempotency_key
    defaults = dict(
        command_id=f"cmd_{uuid.uuid4().hex[:20]}",
        command_type="workflow", owner_user_id=f"user-{_RUN}",
        payload={"goal": "g"}, idempotency_key="k1", request_digest="d1",
    )
    defaults.update(overrides)
    cmd = Command(**defaults)
    cmd.idempotency_scoped = scoped_idempotency_key(
        cmd.owner_user_id, cmd.command_type, cmd.idempotency_key)
    return cmd


def _event(cmd):
    from backend.models.command import OutboxEvent, new_event_id
    return OutboxEvent(event_id=new_event_id(), command_id=cmd.command_id,
                       aggregate_id=cmd.command_id, aggregate_version=1,
                       event_type="command.accepted",
                       payload={"command_id": cmd.command_id})


_ADMIT = SimpleNamespace(max_pending_per_user=4, max_wait_seconds=3600)


@pytest.mark.asyncio
async def test_acceptance_is_durable_and_dedups(repo):
    cmd = _command()
    receipt = await repo.accept_command_with_initial_work(cmd, _ADMIT, _event(cmd))
    assert receipt.accepted is True and receipt.deduplicated is False
    replay = await repo.accept_command_with_initial_work(_command(), _ADMIT,
                                                         _event(cmd))
    assert replay.accepted and replay.deduplicated
    assert replay.command_id == receipt.command_id
    durable = await repo.get_command(receipt.command_id)
    assert durable is not None and durable.state.value == "ACCEPTED"


@pytest.mark.asyncio
async def test_digest_conflict_and_cross_tenant_independence(repo):
    await repo.accept_command_with_initial_work(_command(), _ADMIT, _event(_command()))
    conflicting = _command(request_digest="d-DIFFERENT")
    conflicting.idempotency_scoped = _command().idempotency_scoped
    conflict = await repo.accept_command_with_initial_work(conflicting, _ADMIT,
                                                           _event(conflicting))
    assert conflict.accepted is False
    assert conflict.error_code.value == "IDEMPOTENCY_CONFLICT"

    other = _command(owner_user_id=f"other-{_RUN}")
    independent = await repo.accept_command_with_initial_work(other, _ADMIT,
                                                              _event(other))
    assert independent.accepted and not independent.deduplicated


@pytest.mark.asyncio
async def test_counter_doc_enforces_queue_cap(repo):
    for i in range(2):
        cmd = _command(idempotency_key=f"cap-{i}", request_digest=f"d-{i}")
        r = await repo.accept_command_with_initial_work(cmd, _ADMIT, _event(cmd))
        assert r.accepted
    full = SimpleNamespace(max_pending_per_user=2, max_wait_seconds=3600)
    refused = _command(idempotency_key="cap-over", request_digest="d-over")
    denied = await repo.accept_command_with_initial_work(refused, full,
                                                         _event(refused))
    assert denied.accepted is False
    assert denied.error_code.value == "QUEUE_FULL"
    # Nothing persisted for the refusal.
    assert await repo.get_command(refused.command_id) is None


@pytest.mark.asyncio
async def test_conditional_transition_and_terminal_protection(repo):
    from backend.models.command import CommandState, CommandTransitionError
    cmd = _command()
    await repo.accept_command_with_initial_work(cmd, _ADMIT, _event(cmd))
    c = await repo.transition(cmd.command_id, CommandState.QUEUED, actor="t")
    assert c.state is CommandState.QUEUED and c.state_version == 2
    c = await repo.transition(cmd.command_id, CommandState.RUNNING, actor="t")
    c = await repo.transition(cmd.command_id, CommandState.SUCCEEDED, actor="t")
    assert c.terminal
    with pytest.raises(CommandTransitionError):
        await repo.transition(cmd.command_id, CommandState.FAILED, actor="t")


@pytest.mark.asyncio
async def test_cancellation_intent_is_idempotent_and_honest(repo):
    from backend.models.command import CommandState
    cmd = _command()
    await repo.accept_command_with_initial_work(cmd, _ADMIT, _event(cmd))
    await repo.transition(cmd.command_id, CommandState.RUNNING, actor="t")
    c1 = await repo.record_cancellation_intent(cmd.command_id, "user", "reason")
    assert c1.state is CommandState.CANCEL_REQUESTED
    c2 = await repo.record_cancellation_intent(cmd.command_id, "user", "reason")
    assert c2.state_version == c1.state_version   # duplicate: same result
    c3 = await repo.record_cancellation_intent(cmd.command_id, "user", "late")
    assert c3.state is CommandState.CANCEL_REQUESTED


@pytest.mark.asyncio
async def test_outbox_claim_mark_and_replay(repo):
    cmd = _command()
    await repo.accept_command_with_initial_work(cmd, _ADMIT, _event(cmd))
    claimed = await repo.claim_outbox_batch("d1", 10, 0)   # zero lease: lapsed
    assert len(claimed) == 1
    # Crash before the marker: recovery re-claims and re-publishes.
    await repo.reclaim_stale_outbox_leases()
    claimed2 = await repo.claim_outbox_batch("d2", 10, 60)
    assert [e.event_id for e in claimed2] == [e.event_id for e in claimed]
    assert await repo.mark_outbox_published(claimed2[0].event_id) is True
    assert await repo.claim_outbox_batch("d3", 10, 60) == []
