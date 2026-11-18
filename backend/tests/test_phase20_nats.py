"""Phase 20 §20.5: NATS JetStream adapter — shaping and refusal
contracts are verified deterministically; live delivery is verified
against a real server when one is reachable (compose profile `core`),
and otherwise reported BLOCKED with the runnable command, never faked."""
import asyncio
import uuid

import pytest

from backend.models.schemas import TaskTriggerEvent


@pytest.fixture
def bus():
    from backend.repositories.nats_message_bus import NatsMessageBus
    return NatsMessageBus(["nats://127.0.0.1:4222"])


class FakeMsg:
    def __init__(self, data=b'{"x":1}', delivered=1):
        self.data = data
        self.acked = self.termed = self.nakked = False
        self.nak_delay = None
        self.metadata = type("Meta", (), {
            "num_delivered": delivered,
            "sequence": type("S", (), {"stream": 1, "consumer": 1})(),
        })()

    async def ack(self):
        self.acked = True

    async def nak(self, delay=None):
        self.nakked = True
        self.nak_delay = delay

    async def term(self):
        self.termed = True


class FakePullSub:
    def __init__(self):
        self.fetched = 0
        self.msgs = [FakeMsg()]

    async def fetch(self, batch=10, timeout=5):
        self.fetched += 1
        if self.msgs:
            return self.msgs
        await asyncio.sleep(3600)   # idle like a real pull fetch


class FakeJS:
    def __init__(self):
        self.published = []
        self.subscribe_kwargs = None
        self.pull_sub = FakePullSub()

    async def publish(self, subject, data, **kw):
        self.published.append((subject, data, kw))

    async def pull_subscribe(self, *a, **kw):
        self.subscribe_kwargs = kw
        return self.pull_sub

    async def consumer_info(self, *a, **kw):
        return object()


# ── shaping/fallback contracts (deterministic) ─────────────────────────

@pytest.mark.asyncio
async def test_publish_before_connect_refuses_loudly(bus):
    """No silent fallback: an unconnected bus raises rather than losing
    durability into an in-memory queue."""
    with pytest.raises(RuntimeError, match="not connected"):
        await bus.publish("workflow.events", {"event_id": "e1"})
    with pytest.raises(RuntimeError, match="not connected"):
        await bus.consume("workflow.events", _noop, durable_name="d1")


async def _noop(payload, ctx):
    return None


@pytest.mark.asyncio
async def test_connect_reports_unreachable_server_honestly(bus):
    """With no NATS server on :4222, connect() returns False (honest
    BLOCKED evidence for the live gate) — it neither fakes success nor
    hangs. When a server IS present, the full publish→consume→ack
    journey runs live."""
    ok = await bus.connect()
    if not ok:
        pytest.skip("BLOCKED: no NATS server on 127.0.0.1:4222 — "
                    "run `docker compose --profile core up nats` for the "
                    "live delivery gate")
    # Unique identities per run: the stream's server-side dedup window
    # would otherwise swallow a re-published event with the same id.
    suffix = uuid.uuid4().hex[:8]
    await bus.publish("workflow.events",
                      TaskTriggerEvent(workflow_id="wf-1", run_id="r-1",
                                       task_id=f"t-nats-{suffix}"))
    received = []

    async def handler(payload, ctx):
        received.append(payload)
        await ctx.ack()

    await bus.consume("workflow.events", handler,
                      durable_name=f"t-nats-{suffix}")
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.1)
    # A fresh durable starts at the stream head, so earlier runs' events
    # may be delivered first; what matters is that THIS run's event is
    # delivered and acked (at-least-once, in-order per consumer).
    task_ids = {payload.get("task_id") for payload in received}
    assert f"t-nats-{suffix}" in task_ids, \
        f"published event not delivered; got {sorted(task_ids)[-3:]}"
    await bus.close()


def test_encode_prefers_pydantic_dump_and_stable_event_id():
    from backend.repositories.nats_message_bus import NatsMessageBus
    evt = TaskTriggerEvent(workflow_id="wf-1", run_id="r-1", task_id="t-9")
    raw = NatsMessageBus._encode(evt)
    assert b'"t-9"' in raw
    decoded = NatsMessageBus._decode(raw)
    assert decoded["task_id"] == "t-9"


@pytest.mark.asyncio
async def test_publish_sets_nats_msg_id_dedup_header():
    from backend.repositories.nats_message_bus import NatsMessageBus
    js = FakeJS()
    b = NatsMessageBus(["nats://127.0.0.1:4222"])
    b._js = js  # shape test: publish path only
    await b.publish("workflow.events",
                    {"event_id": "evt-42", "task_id": "t-1"})
    subject, data, kw = js.published[0]
    assert subject == "agentos.workflow.events"
    assert kw["headers"]["Nats-Msg-Id"] == "evt-42", \
        "server-side dedup is keyed on the event identity"
    # Falls back to task_id then topic for identity.
    await b.publish("workflow.events", {"task_id": "t-2"})
    assert js.published[1][2]["headers"]["Nats-Msg-Id"] == "t-2"
    await b.publish("workflow.events", {"n": 3})
    assert js.published[2][2]["headers"]["Nats-Msg-Id"] == "workflow.events"


# ── context contract: ack / bounded retry / dead-letter ────────────────

@pytest.mark.asyncio
async def test_context_ack_maps_to_jetstream_ack():
    from backend.repositories.nats_message_bus import NatsMessageContext
    msg = FakeMsg()
    ctx = NatsMessageContext(msg, js=None, durable_name="d")
    await ctx.ack()
    assert msg.acked and not msg.nakked and not msg.termed
    await ctx.ack()  # idempotent settle
    assert ctx.settled


@pytest.mark.asyncio
async def test_context_nak_retries_below_delivery_cap():
    from backend.repositories.nats_message_bus import NatsMessageContext
    msg = FakeMsg(delivered=2)
    ctx = NatsMessageContext(msg, js=None, durable_name="d")
    await ctx.nack()
    assert msg.nakked and not msg.termed, "below cap: redelivery, not DLQ"
    assert msg.nak_delay == 2


@pytest.mark.asyncio
async def test_context_dead_letters_after_bounded_retries():
    """After MAX_DELIVER attempts a poison message goes to the DLQ
    subject and is TERMINATED — never an infinite retry loop."""
    from backend.repositories.nats_message_bus import (
        DEAD_LETTER_PREFIX, MAX_DELIVER, NatsMessageContext)
    js = FakeJS()
    msg = FakeMsg(delivered=MAX_DELIVER)
    ctx = NatsMessageContext(msg, js=js, durable_name="worker-pool")
    await ctx.nack()
    assert js.published[0][0] == f"{DEAD_LETTER_PREFIX}worker-pool"
    assert msg.termed and not msg.nakked, "capped attempts terminate"
    assert ctx.settled, "a dead-lettered message is settled exactly once"


@pytest.mark.asyncio
async def test_context_retryable_failure_never_dead_letters():
    from backend.repositories.nats_message_bus import (
        MAX_DELIVER, NatsMessageContext)
    js = FakeJS()
    msg = FakeMsg(delivered=MAX_DELIVER - 1)
    ctx = NatsMessageContext(msg, js=js, durable_name="worker-pool")
    await ctx.nack()
    assert js.published == [], "no DLQ publish while retries remain"
    assert msg.nakked and not msg.termed


@pytest.mark.asyncio
async def test_pull_consumer_config_is_bounded_and_manual_ack():
    """The pull consumer carries the SAME delivery contract: manual ack,
    bounded redelivery, ack window — the worker pool shares it."""
    from backend.repositories.nats_message_bus import MAX_DELIVER
    from backend.repositories.nats_message_bus import NatsMessageBus

    b = NatsMessageBus(["nats://127.0.0.1:4222"])
    js = FakeJS()
    b._js = js
    task = await b.consume("t", _noop, durable_name="d")
    cfg = js.subscribe_kwargs["config"]
    assert cfg.max_deliver == MAX_DELIVER
    assert cfg.ack_wait == 60
    assert cfg.durable_name == "d"
    assert js.subscribe_kwargs["durable"] == "d"
    task.cancel()   # stop the fetch loop
    import asyncio as _a
    try:
        await task
    except _a.CancelledError:
        pass
