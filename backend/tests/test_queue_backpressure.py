"""Phase 3 §3.21: durable command admission / queue backpressure (§3.12).

The strict queue counter is the pending_commands table — NOT a Pub/Sub
backlog approximation. Under test: per-tenant pending caps, per-class caps,
max-wait expiry, duplicate command deduplication, and the settled/expired
lifecycle that frees slots.
"""
import asyncio
import time

import pytest
import pytest_asyncio

from backend.models.admission import AdmissionReason
from backend.repositories.admission_repository import (
    MAX_COMMAND_WAIT_SECONDS,
    SQLiteCommandAdmissionRepository,
)
from backend.services import admission_service


@pytest_asyncio.fixture
async def db(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "commands.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def repo(db):
    return SQLiteCommandAdmissionRepository(db)


@pytest_asyncio.fixture
async def wired(db, repo):
    from backend.repositories.admission_repository import set_command_admission_repo
    set_command_admission_repo(repo)
    yield repo
    set_command_admission_repo(None)


@pytest.mark.asyncio
async def test_tenant_pending_cap_rejects_with_queue_full(repo):
    for i in range(3):
        result = await repo.admit_command(f"c-{i}", "u1", None, "workflow",
                                          max_pending_per_user=3,
                                          max_pending_per_org=0,
                                          max_pending_per_class=0,
                                          max_wait_seconds=3600)
        assert result.accepted
    rejected = await repo.admit_command("c-late", "u1", None, "workflow",
                                        max_pending_per_user=3,
                                        max_pending_per_org=0,
                                        max_pending_per_class=0,
                                        max_wait_seconds=3600)
    assert rejected.accepted is False
    assert rejected.reason == AdmissionReason.QUEUE_FULL
    assert rejected.retry_after_seconds > 0


@pytest.mark.asyncio
async def test_other_tenants_retain_access_when_one_floods(repo):
    """§3.21: one tenant at its cap cannot consume another's queue slots."""
    for i in range(2):
        await repo.admit_command(f"flood-{i}", "flood-user", None, "workflow",
                                 max_pending_per_user=2, max_pending_per_org=0,
                                 max_pending_per_class=0, max_wait_seconds=3600)
    other = await repo.admit_command("other-1", "calm-user", None, "workflow",
                                     max_pending_per_user=2, max_pending_per_org=0,
                                     max_pending_per_class=0, max_wait_seconds=3600)
    assert other.accepted is True


@pytest.mark.asyncio
async def test_duplicate_command_returns_existing_without_new_slot(repo):
    first = await repo.admit_command("cmd-1", "u1", None, "workflow",
                                     max_pending_per_user=1, max_pending_per_org=0,
                                     max_pending_per_class=0, max_wait_seconds=3600)
    assert first.accepted and not first.duplicate
    again = await repo.admit_command("cmd-1", "u1", None, "workflow",
                                     max_pending_per_user=1, max_pending_per_org=0,
                                     max_pending_per_class=0, max_wait_seconds=3600)
    assert again.accepted is True
    assert again.duplicate is True
    assert again.first_command_id == "cmd-1"
    assert (await repo.pending_counts(user_id="u1")) == 1


@pytest.mark.asyncio
async def test_idempotency_key_dedups_across_command_ids(repo):
    first = await repo.admit_command("cmd-A", "u1", None, "build",
                                     max_pending_per_user=5, max_pending_per_org=0,
                                     max_pending_per_class=0, max_wait_seconds=3600,
                                     idempotency_key="build:abc")
    second = await repo.admit_command("cmd-B", "u1", None, "build",
                                      max_pending_per_user=5, max_pending_per_org=0,
                                      max_pending_per_class=0, max_wait_seconds=3600,
                                      idempotency_key="build:abc")
    assert first.accepted and second.accepted and second.duplicate is True
    assert second.first_command_id == "cmd-A"


@pytest.mark.asyncio
async def test_settled_command_frees_its_queue_slot(repo):
    await repo.admit_command("cmd-1", "u1", None, "workflow",
                             max_pending_per_user=1, max_pending_per_org=0,
                             max_pending_per_class=0, max_wait_seconds=3600)
    assert (await repo.pending_counts(user_id="u1")) == 1
    await repo.settle_command("cmd-1")
    assert (await repo.pending_counts(user_id="u1")) == 0
    # The freed slot admits new work again.
    fresh = await repo.admit_command("cmd-2", "u1", None, "workflow",
                                     max_pending_per_user=1, max_pending_per_org=0,
                                     max_pending_per_class=0, max_wait_seconds=3600)
    assert fresh.accepted is True


@pytest.mark.asyncio
async def test_max_wait_expiry_frees_slots(repo):
    await repo.admit_command("cmd-stale", "u1", None, "workflow",
                             max_pending_per_user=1, max_pending_per_org=0,
                             max_pending_per_class=0, max_wait_seconds=60)
    swept = await repo.expire_stale(now_epoch=int(time.time()) + 120)
    assert swept == 1
    fresh = await repo.admit_command("cmd-fresh", "u1", None, "workflow",
                                     max_pending_per_user=1, max_pending_per_org=0,
                                     max_pending_per_class=0, max_wait_seconds=3600)
    assert fresh.accepted is True


@pytest.mark.asyncio
async def test_max_wait_is_clamped_to_policy_ceiling(repo):
    result = await repo.admit_command("cmd-long", "u1", None, "workflow",
                                      max_pending_per_user=5, max_pending_per_org=0,
                                      max_pending_per_class=0,
                                      max_wait_seconds=999_999_999)
    assert result.expires_at_epoch - int(time.time()) <= MAX_COMMAND_WAIT_SECONDS + 2


@pytest.mark.asyncio
async def test_workload_class_cap(repo):
    for i in range(2):
        await repo.admit_command(f"b-{i}", "u1", None, "build",
                                 max_pending_per_user=0, max_pending_per_org=0,
                                 max_pending_per_class=2, max_wait_seconds=3600)
    rejected = await repo.admit_command("b-late", "u1", None, "build",
                                        max_pending_per_user=0,
                                        max_pending_per_org=0,
                                        max_pending_per_class=2,
                                        max_wait_seconds=3600)
    assert rejected.accepted is False
    # A different class is unaffected by the build class's full queue.
    workflow = await repo.admit_command("w-1", "u1", None, "workflow",
                                        max_pending_per_user=0,
                                        max_pending_per_org=0,
                                        max_pending_per_class=2,
                                        max_wait_seconds=3600)
    assert workflow.accepted is True


@pytest.mark.asyncio
async def test_service_wiring_end_to_end(wired):
    result = await admission_service.admit_command("svc-1", "u1", None,
                                                   "workflow",
                                                   max_pending_per_user=2,
                                                   max_pending_per_org=0,
                                                   max_pending_per_class=0)
    assert result.accepted is True
    await admission_service.settle_command("svc-1")
    assert (await admission_service.expire_stale()) == 0


@pytest.mark.asyncio
async def test_service_without_repo_admits_locally():
    from backend.repositories.admission_repository import set_command_admission_repo
    set_command_admission_repo(None)
    result = await admission_service.admit_command("local-1", "u1", None,
                                                   "workflow")
    assert result.accepted is True   # documented local-mode posture


# ═══ §3.13: bounded local message bus — no unbounded growth, no deadlock ═


def _bus(monkeypatch, queue_maxsize=3, retry_limit=4, dlq_maxsize=2,
         put_timeout=0.2, retry_age=900):
    from backend.repositories.in_memory_message_bus import InMemoryMessageBus
    from backend.config.settings import settings
    monkeypatch.setattr(settings, "LOCAL_QUEUE_MAXSIZE", queue_maxsize)
    monkeypatch.setattr(settings, "LOCAL_MAX_PENDING_RETRIES", retry_limit)
    monkeypatch.setattr(settings, "LOCAL_DEAD_LETTER_MAXSIZE", dlq_maxsize)
    monkeypatch.setattr(settings, "LOCAL_QUEUE_PUT_TIMEOUT", put_timeout)
    monkeypatch.setattr(settings, "LOCAL_MAX_RETRY_AGE_SECONDS", retry_age)
    monkeypatch.setattr(settings, "LOCAL_WORKER_CONCURRENCY", 2)
    return InMemoryMessageBus(max_deliveries=2, retry_base_seconds=0.01)


def _payload(i=0, attempt=1):
    return {"delivery_attempt": attempt, "message": {"task_id": f"t-{i}"}}


@pytest.mark.asyncio
async def test_full_queue_rejects_publish_after_timeout(monkeypatch):
    """A full bounded queue is a REAL capacity signal: the publisher waits
    briefly then receives MessageBusFullError — never silent unbounded growth."""
    from backend.repositories.in_memory_message_bus import MessageBusFullError
    bus = _bus(monkeypatch, queue_maxsize=1, put_timeout=0.2)
    await bus.publish("topic-x", _payload(1))
    with pytest.raises(MessageBusFullError):
        await bus.publish("topic-x", _payload(2))   # no consumer: stays full
    assert bus.metrics["publish_rejected"] == 1


@pytest.mark.asyncio
async def test_all_consumers_retrying_cannot_deadlock(monkeypatch):
    """§3.21 'All consumers retry into a full queue → no deadlock': a retry
    is parked in the bounded retry store; the pump moves it without ever
    blocking on a full queue."""
    bus = _bus(monkeypatch, queue_maxsize=1, retry_limit=10)
    topic = "topic-retry"
    queue = bus._get_queue(topic)
    for i in range(5):
        # Queue FULL: retry scheduling must still succeed (never blocks).
        bus.schedule_retry(topic, _payload(i), delay=0.01)
    assert len(bus._retry_store) == 5
    # Pump drains into the bounded queue up to its capacity, park the rest.
    await asyncio.sleep(0.4)
    drained = 0
    while not queue.empty():
        queue.get_nowait()
        queue.task_done()
        drained += 1
    assert 0 < drained <= 1
    assert len(bus._retry_store) == 5 - drained   # rest parked, not lost


@pytest.mark.asyncio
async def test_retry_store_overflow_deadletters(monkeypatch):
    bus = _bus(monkeypatch, queue_maxsize=1, retry_limit=2, dlq_maxsize=10)
    for i in range(4):
        bus.schedule_retry("topic-ovf", _payload(i), delay=60)
    # Two parked, the rest promoted to dead letters (bounded retry storage).
    assert len(bus._retry_store) == 2
    assert bus.metrics["retry_overflow_deadlettered"] >= 1
    await asyncio.sleep(0.05)   # fire-and-forget dead-letter writes flush
    assert not bus.dead_letters.empty()


@pytest.mark.asyncio
async def test_dead_letter_queue_is_bounded_oldest_dropped(monkeypatch):
    bus = _bus(monkeypatch, queue_maxsize=5, dlq_maxsize=2)
    for i in range(5):
        await bus._dead_letter("topic-dlq", {"n": i}, 3, f"m-{i}")
    assert bus.dead_letters.qsize() == 2
    assert bus.metrics["dead_letter_dropped_oldest"] == 3
    # Newest evidence retained.
    entries = []
    while not bus.dead_letters.empty():
        entries.append(bus.dead_letters.get_nowait())
    assert [e["message_id"] for e in entries] == ["m-3", "m-4"]


@pytest.mark.asyncio
async def test_retry_age_limit_deadletters_stale_retries(monkeypatch):
    bus = _bus(monkeypatch, retry_age=10)
    payload = _payload(0)
    payload["_retry_enqueued_at"] = time.time() - 60   # already too old
    bus.schedule_retry("topic-age", payload, delay=0.01)
    assert len(bus._retry_store) == 0
    await asyncio.sleep(0.05)
    assert bus.metrics["retry_age_deadlettered"] == 1
    assert not bus.dead_letters.empty()


@pytest.mark.asyncio
async def test_consumer_nack_with_full_queue_never_blocks(monkeypatch):
    """The §3.13 deadlock scenario: queue full, a consumer nacks — the
    nack returns immediately (retry parked), the consumer loop continues."""
    bus = _bus(monkeypatch, queue_maxsize=1)
    topic = "topic-nack"
    queue = bus._get_queue(topic)
    queue.put_nowait({"message": {"task_id": "held"}, "carrier": {}})
    from backend.repositories.in_memory_message_bus import InMemoryMessageContext
    message = {"delivery_attempt": 1, "message": {"task_id": "n-1"}, "carrier": {}}
    context = InMemoryMessageContext("m-1", queue, message, bus, topic,
                                     max_deliveries=2, retry_base_seconds=0.01)
    await asyncio.wait_for(context.nack(), timeout=1.0)   # must NOT hang
    assert len(bus._retry_store) == 1
    assert context.settled is True


@pytest.mark.asyncio
async def test_shutdown_with_pending_messages_cancels_pump_cleanly(monkeypatch):
    """Shutdown with pending retries: the pump task is cancelled by the
    TaskGroup — no orphaned requeues, no unretrieved-task warnings."""
    bus = _bus(monkeypatch, queue_maxsize=5)
    bus.schedule_retry("topic-shut", _payload(0), delay=5)   # won't fire soon
    got = {}

    async def handler(message, ctx):
        got["called"] = True
        await ctx.ack()

    consumer = asyncio.create_task(bus.consume("topic-shut", handler))
    await asyncio.sleep(0.1)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass
    assert bus._pump_task.cancelled() or bus._pump_task.done()
    assert "called" not in got
