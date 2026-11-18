"""NATS JetStream durable message bus (Phase 20 §20.5).

Durable file-backed stream, explicit acks, bounded redelivery with a
dead-letter subject, server-side deduplication via ``Nats-Msg-Id``, and
an ordered replay path for reconciliation. At-least-once delivery is the
transport contract: AgentOS command/effect handling stays idempotent.

The adapter refuses to publish or consume before a successful connect —
there is no silent fallback to an in-memory bus, because losing
durability silently is worse than failing loudly. ``connect()`` returns
False when no server is reachable so callers can choose the transport
explicitly (compose profile ``core`` starts a NATS server on :4222).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any, Awaitable, Callable, List, Optional

from backend.repositories.message_bus import MessageBus, MessageContext

logger = logging.getLogger("nats_message_bus")

DEFAULT_STREAM = "agentos"
DEAD_LETTER_PREFIX = "agentos.dlq."
MAX_DELIVER = 5  # bounded redelivery before dead-lettering
NAK_DELAY_SECONDS = 2


class NatsMessageContext(MessageContext):
    """Maps the ABC ack/nack contract onto JetStream messages, with
    dead-lettering after MAX_DELIVER attempts (§20.5)."""

    def __init__(self, msg: Any, js: Any, durable_name: str):
        self._msg = msg
        self._js = js
        self._durable = durable_name
        self.settled = False

    def attempts(self) -> int:
        meta = getattr(self._msg, "metadata", None)
        return int(getattr(meta, "num_delivered", 1)) if meta else 1

    async def ack(self) -> None:
        if not self.settled:
            self.settled = True
            await self._msg.ack()

    async def nack(self) -> None:
        """Retry (bounded): nak with delay; dead-letter + term once the
        delivery cap is exhausted so poison messages never loop."""
        if self.settled:
            return
        if self.attempts() >= MAX_DELIVER:
            dlq = f"{DEAD_LETTER_PREFIX}{self._durable}"
            await self._js.publish(dlq, self._msg.data)
            await self._msg.term()
            self.settled = True
            logger.error("Message dead-lettered after %d attempts on %s",
                         self.attempts(), self._durable)
        else:
            await self._msg.nak(delay=NAK_DELAY_SECONDS)


class NatsMessageBus(MessageBus):
    """JetStream-backed MessageBus with the publish/consume contract of
    the in-process bus."""

    def __init__(self, servers: List[str], *,
                 stream: str = DEFAULT_STREAM,
                 ack_wait_seconds: int = 60,
                 startup_timeout: float = 10):
        if not math.isfinite(startup_timeout) or startup_timeout <= 0:
            raise ValueError("startup_timeout must be finite and positive")
        self._startup_timeout = startup_timeout
        self._servers = servers
        self._stream = stream
        self._ack_wait = ack_wait_seconds
        self._nc: Optional[Any] = None
        self._js: Optional[Any] = None
        self._subscriptions: List[Any] = []

    @staticmethod
    def _encode(message: Any) -> bytes:
        if hasattr(message, "model_dump"):
            message = message.model_dump(mode="json")
        elif hasattr(message, "to_dict"):
            message = message.to_dict()
        return json.dumps(message, separators=(",", ":"), default=str).encode("utf-8")

    @staticmethod
    def _decode(data: bytes) -> Any:
        return json.loads(data.decode("utf-8"))

    async def connect(self) -> bool:
        """Connect and ensure the durable stream. False when the server
        is unreachable — the caller picks a transport explicitly."""
        if self.connected():
            return True
        try:
            from nats.aio.client import Client
            # Keep the client handle before connecting so timeout/cancellation
            # can close a partially initialized connection and its tasks.
            self._nc = Client()
            await asyncio.wait_for(
                self._nc.connect(servers=self._servers, connect_timeout=5),
                timeout=self._startup_timeout)
            self._js = self._nc.jetstream()
            # `>` matches one-or-more tokens: workflow.events lands on
            # the stream (a single `*` would match only agentos.<one>).
            try:
                await self._js.add_stream(
                    name=self._stream,
                    subjects=[f"{self._stream}.>"],
                    storage="file",
                    retention="limits")
            except Exception as exc:
                # A stream left by an earlier deployment may carry a
                # different config ("already in use with a different
                # configuration"): reconcile it to the CURRENT subject
                # set rather than publishing into a dead wildcard.
                try:
                    from nats.js.api import StreamConfig
                    await self._js.update_stream(StreamConfig(
                        name=self._stream,
                        subjects=[f"{self._stream}.>"],
                        storage="file",
                        retention="limits"))
                    logger.info("NATS stream %s reconciled to %s.>",
                                self._stream, self._stream)
                except Exception as recon_exc:
                    logger.error("Stream %s unusable after add failure "
                                 "(add: %s; reconcile: %s)", self._stream,
                                 str(exc)[:120], str(recon_exc)[:120])
                    raise
            return True
        except asyncio.CancelledError:
            await self._close_failed_connection()
            raise
        except Exception as exc:
            logger.warning("NATS connect failed (%s): %s",
                           self._servers, str(exc)[:200])
            await self._close_failed_connection()
            return False

    async def _close_failed_connection(self) -> None:
        client = self._nc
        self._nc = None
        self._js = None
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), timeout=2)
            except Exception:
                logger.warning("NATS failed connection cleanup did not complete")

    def connected(self) -> bool:
        return self._js is not None

    def _require_connection(self) -> None:
        if self._js is None:
            raise RuntimeError(
                "NATS not connected — transport operation refused "
                "(no silent fallback to an in-memory bus)")

    async def publish(self, topic: str, message: Any) -> None:
        self._require_connection()
        event_id = ""
        if hasattr(message, "model_dump"):
            dumped = message.model_dump(mode="json")
        else:
            dumped = message if isinstance(message, dict) else {}
        event_id = str(dumped.get("event_id") or dumped.get("task_id")
                       or topic)
        await self._js.publish(f"{self._stream}.{topic}",
                               self._encode(message),
                               headers={"Nats-Msg-Id": event_id})

    async def consume(self, topic: str,
                      handler: Callable[[Any, MessageContext], Awaitable[None]],
                      *, durable_name: Optional[str] = None,
                      max_deliver: int = MAX_DELIVER) -> Any:
        """Durable PULL consumer (§21.4): a worker pool shares the durable
        consumer with server-side rebalancing — a restarted worker simply
        re-attaches (push consumers bind to ONE subscriber, which is why
        the old push design wedged after restarts). A background task
        fetches batches, dispatches to the handler, and applies the
        ack/nak/dead-letter contract."""
        self._require_connection()
        durable_name = durable_name or f"d-{topic.replace('.', '-')}"
        from nats.js.api import ConsumerConfig
        from nats.errors import TimeoutError as NatsTimeoutError
        import asyncio as _asyncio

        subject = f"{self._stream}.{topic}"
        try:
            sub = await self._js.pull_subscribe(
                subject, durable=durable_name,
                config=ConsumerConfig(durable_name=durable_name,
                                      ack_wait=self._ack_wait,
                                      max_deliver=max_deliver))
        except Exception as exc:
            # A consumer left by an earlier deployment with a different
            # config: reconcile it to the current contract.
            try:
                await self._js.consumer_info(self._stream, durable_name)
                sub = await self._js.pull_subscribe(subject,
                                                    durable=durable_name)
            except Exception as inner:
                raise RuntimeError(
                    f"cannot attach durable consumer {durable_name!r} on "
                    f"{subject}: {exc}; {inner}") from inner

        async def _fetch_loop():
            while True:
                try:
                    msgs = await sub.fetch(batch=10, timeout=5)
                except NatsTimeoutError:
                    continue
                except _asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("pull fetch failed: %s", str(exc)[:160])
                    await _asyncio.sleep(1)
                    continue
                for msg in msgs:
                    ctx = NatsMessageContext(msg, self._js, durable_name)
                    try:
                        payload = self._decode(msg.data)
                        await handler(payload, ctx)
                    except Exception as exc:
                        logger.warning("handler failed on %s (attempt %d): %s",
                                       durable_name, ctx.attempts(),
                                       str(exc)[:160])
                        try:
                            await ctx.nack()
                        except Exception:
                            pass
                        continue
                    if not ctx.settled:
                        try:
                            await ctx.ack()
                        except Exception:
                            pass

        task = _asyncio.create_task(_fetch_loop())
        self._subscriptions.append(task)
        return task

    async def replay(self, topic: str,
                     handler: Callable[[Any, MessageContext], Awaitable[None]],
                     *, start_seq: int = 1) -> int:
        """§20.5 replay/reconciliation: ordered redelivery from a stream
        sequence through an ephemeral ordered consumer. Returns the count
        of events handled."""
        self._require_connection()
        sub = await self._js.subscribe(f"{self._stream}.{topic}",
                                       ordered_consumer=True)
        count = 0
        try:
            msg = await sub.next_msg(timeout=5)
            while msg is not None:
                if msg.metadata.sequence.stream >= start_seq:
                    payload = self._decode(msg.data)
                    ctx = NatsMessageContext(msg, self._js, "replay")
                    await handler(payload, ctx)
                    count += 1
                msg = await sub.next_msg(timeout=1)
        except Exception:
            pass  # timeout ends the replay
        finally:
            try:
                await sub.unsubscribe()
            except Exception:
                pass
        return count

    async def close(self) -> None:
        for sub in self._subscriptions:
            try:
                if hasattr(sub, "cancel"):
                    sub.cancel()
                else:
                    await sub.unsubscribe()
            except Exception:
                pass
        self._subscriptions.clear()
        if self._nc is not None:
            await self._nc.close()
            self._nc = None
            self._js = None
