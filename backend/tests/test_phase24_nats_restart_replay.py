"""Phase 24 §24.4 — broker recreation + outbox replay drill.

The system's durable-delivery contract: the POSTGRES transactional
outbox is the source of truth; NATS JetStream is the transport (its
state is transient by design — bounded redelivery, no promise that
recent ack state survives a kill). The §24.4 drill therefore drives the
real chain end to end:

1. A command persists with its outbox event (durable row).
2. Worker A claims the event, publishes to NATS, and a consumer
   receives it — but A never records delivery/acks (it dies).
3. The broker is RESTARTED (recreated state).
4. The outbox reconciler reclaims the stale lease; worker B claims the
   SAME event and republishes.
5. A fresh consumer receives the event again; the effect layer's
   idempotency ledger proves the consequential effect executed EXACTLY
   ONCE despite the redelivery.

Runnable: pytest backend/tests/test_phase24_nats_restart_replay.py -q
"""
import json
import subprocess
import uuid

import pytest


def _nats_up(port=4222) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def _pg_up() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 55432), timeout=3):
            return True
    except OSError:
        return False


@pytest.fixture
def broker():
    # Use the installed image but never restart the application's broker.
    image = subprocess.run(["docker", "inspect", "agentos-nats-1", "--format", "{{.Image}}"],
                           capture_output=True, text=True)
    if image.returncode:
        pytest.skip("BLOCKED: local NATS image/daemon unavailable")
    name = "agentos-p25-nats-" + uuid.uuid4().hex[:12]
    def start(port=""):
        subprocess.run(["docker", "run", "--rm", "-d", "--name", name,
                        "-p", f"127.0.0.1:{port}:4222", image.stdout.strip(), "-js"],
                       capture_output=True, check=True, timeout=30)
    try:
        start()
        result = subprocess.run(["docker", "port", name, "4222"], capture_output=True,
                                text=True, check=True)
        port = int(result.stdout.strip().rsplit(":", 1)[1])
        yield name, port, start
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def test_broker_recreation_then_outbox_replay_delivers_exactly_once(broker):
    broker_name, broker_port, start_broker = broker
    if not _pg_up():
        pytest.skip("BLOCKED: no Postgres on 127.0.0.1:55432 — "
                    "`docker run ... postgres:16-alpine`")
    import asyncio
    import time
    from backend.repositories.nats_message_bus import NatsMessageBus
    from backend.repositories.postgres.database import PostgresDatabaseManager
    from backend.repositories.postgres.repositories import (
        PostgresCommandRepository, PostgresIdempotencyRepository)
    from backend.services.command_service import set_command_repo

    DSN = "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test"
    suffix = uuid.uuid4().hex[:10]
    event_id = f"evt-replay-{suffix}"

    async def scenario():
        mgr = PostgresDatabaseManager(DSN, schema="p25_replay_" + suffix)
        await mgr.initialize()
        cmd_repo = PostgresCommandRepository(mgr)
        set_command_repo(cmd_repo)
        try:
            # 1. Durable command + outbox event.
            cmd_id = await cmd_repo.record_command({
                "id": f"cmd-{suffix}", "user_id": "p24-user",
                "run_id": f"run-{suffix}", "command_type": "tool.invoke",
                "payload": {"tool": "p24.tool"},
                "idempotency_key": f"idem-{suffix}"})
            event_row = f"evt-{suffix}"
            await cmd_repo.append_outbox(event_row, "workflow.events",
                                         {"command_id": cmd_id, "n": 1})

            # 2. Worker A claims, publishes, consumer receives, A dies.
            await cmd_repo.claim_outbox_batch("worker-A", lease_seconds=0)
            bus = NatsMessageBus([f"127.0.0.1:{broker_port}"], startup_timeout=15)
            assert await bus.connect()
            await bus.publish("workflow.events",
                              {"command_id": cmd_id, "event_id": event_id})
            first = {}

            async def never_ack(payload, ctx):
                if payload.get("event_id") == event_id:
                    first["ok"] = True
                # NO ack.

            t = asyncio.ensure_future(bus.consume(
                "workflow.events", never_ack,
                durable_name=f"p24-replay-{suffix}"))
            for _ in range(40):
                if first:
                    break
                await asyncio.sleep(0.5)
            t.cancel()
            assert first, "consumer must receive the event before A dies"
            await bus.close()

            # The consequence executes ONCE at the paid boundary.
            idem = PostgresIdempotencyRepository(mgr)
            calls = []

            async def external_effect():
                calls.append(1)
                return {"applied": True, "receipt": f"rcpt-{suffix}"}

            await idem.get_or_put(f"effect:{suffix}", "effect",
                                  external_effect)
            assert len(calls) == 1

            # 3. Restart (recreate) the broker.
            r = subprocess.run(["docker", "rm", "-f", broker_name],
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 0
            start_broker(str(broker_port))
            for _ in range(30):
                if _nats_up(broker_port):
                    break
                time.sleep(1)

            # 4. Outbox reconciliation: stale lease reclaimed; worker B
            #    claims the SAME event and republishes.
            await cmd_repo.reclaim_stale_outbox_leases()
            batch_b = await cmd_repo.claim_outbox_batch("worker-B")
            redelivered_events = [e for e in batch_b
                                  if e.get("event_id") == event_row]
            assert redelivered_events, \
                "the outbox reconciler must hand the event to worker B"
            bus2 = NatsMessageBus([f"127.0.0.1:{broker_port}"], startup_timeout=15)
            assert await bus2.connect()
            await bus2.publish("workflow.events",
                               {"command_id": cmd_id, "event_id": event_id})

            # 5. A fresh consumer receives the replayed event; the effect
            #    ledger proves exactly-once.
            replay = {}

            async def ack_after_effect(payload, ctx):
                if payload.get("event_id") == event_id:
                    replay["ok"] = True
                    _, created = await idem.get_or_put(
                        f"effect:{suffix}", "effect", external_effect)
                    assert not created, \
                        "redelivery must never re-execute the consequence"
                await ctx.ack()

            t2 = asyncio.ensure_future(bus2.consume(
                "workflow.events", ack_after_effect,
                durable_name=f"p24-replay2-{suffix}"))
            for _ in range(60):
                if replay:
                    break
                await asyncio.sleep(1)
            t2.cancel()
            await bus2.close()
            assert replay, ("the replayed event must be delivered after "
                            "broker recreation")
            assert len(calls) == 1, \
                f"exactly one consequential effect expected, got {len(calls)}"
        finally:
            set_command_repo(None)
            await mgr.close(drop_schema=True)

    asyncio.run(scenario())
