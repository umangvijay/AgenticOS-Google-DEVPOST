import asyncio
import logging
import time
from typing import Callable, Awaitable, Any
from backend.models.schemas import TaskTriggerEvent
from backend.repositories.message_bus import MessageBus, MessageContext
from opentelemetry import propagate
from opentelemetry import context as otel_context

logger = logging.getLogger(__name__)


class MessageBusFullError(RuntimeError):
    """Bounded local queue stayed full past the publish timeout (§3.13).
    Callers treat this exactly like a transport rejection — the work was NOT
    accepted, and retrying immediately cannot succeed."""


class InMemoryMessageContext(MessageContext):
    def __init__(self, message_id: str, queue: asyncio.Queue, message: TaskTriggerEvent,
                 bus: "InMemoryMessageBus", topic: str, max_deliveries: int,
                 retry_base_seconds: float):
        self.message_id = message_id
        self.queue = queue
        self.message = message
        self.bus = bus
        self.topic = topic
        self.max_deliveries = max_deliveries
        self.retry_base_seconds = retry_base_seconds
        self.settled = False

    async def ack(self) -> None:
        if self.settled:
            return
        self.settled = True
        logger.debug(f"Message {self.message_id} ACKed")
        self.queue.task_done()

    async def nack(self) -> None:
        """Retry WITHOUT blocking the consumer (§3.13): the pre-Phase-3 path
        slept inside the consumer and then `await queue.put(...)` — with a
        bounded queue, every consumer retrying could deadlock the whole
        topic. The message now goes to the bus's bounded delayed-retry store;
        the pump task moves due entries back and dead-letters overflow."""
        if self.settled:
            return
        self.settled = True
        attempt = int(self.message.get("delivery_attempt", 1))
        if attempt >= self.max_deliveries:
            logger.error("Message %s on %s exhausted %s deliveries; moving to dead-letter queue",
                         self.message_id, self.topic, self.max_deliveries)
            await self.bus._dead_letter(self.topic, self.message.get("message"),
                                        attempt, self.message_id)
            self.queue.task_done()
            return

        delay = self.retry_base_seconds * (2 ** (attempt - 1))
        logger.warning("Message %s NACKed on %s (attempt %s/%s); retrying in %.2fs",
                       self.message_id, self.topic, attempt, self.max_deliveries, delay)
        retry = dict(self.message)
        retry["delivery_attempt"] = attempt + 1
        retry.setdefault("_retry_enqueued_at", time.time())
        self.bus.schedule_retry(self.topic, retry, delay)
        self.queue.task_done()


class InMemoryMessageBus(MessageBus):
    """Bounded local transport (Phase 3 §3.13).

    Local-mode semantics are explicit: this bus provides NO restart
    durability — everything in memory is lost on process exit. Pub/Sub
    remains the durable deployment path.

    Bounds (settings):
    - topic queues: LOCAL_QUEUE_MAXSIZE; publish blocks up to
      LOCAL_QUEUE_PUT_TIMEOUT for space, then raises MessageBusFullError
      (clear rejection — never silent unbounded growth).
    - delayed-retry store: LOCAL_MAX_PENDING_RETRIES entries; overflow
      dead-letters immediately (consumers NEVER block on requeue).
    - dead letters: LOCAL_DEAD_LETTER_MAXSIZE; overflow drops the OLDEST
      entry (bounded memory, newest evidence retained).
    """

    def __init__(self, max_deliveries: int = 5, retry_base_seconds: float = 0.25):
        from backend.config.settings import settings
        self.max_deliveries = max(1, max_deliveries)
        self.retry_base_seconds = max(0, retry_base_seconds)
        self._queue_maxsize = settings.LOCAL_QUEUE_MAXSIZE
        self._put_timeout = settings.LOCAL_QUEUE_PUT_TIMEOUT
        self._retry_store_limit = settings.LOCAL_MAX_PENDING_RETRIES
        self._retry_age_limit = settings.LOCAL_MAX_RETRY_AGE_SECONDS
        self._dlq_maxsize = settings.LOCAL_DEAD_LETTER_MAXSIZE
        self.topics: dict[str, asyncio.Queue] = {}
        self.dead_letters: asyncio.Queue = asyncio.Queue(maxsize=self._dlq_maxsize)
        # Bounded delayed-retry store: (ready_at, seq, payload)
        self._retry_store: list[tuple[float, int, dict]] = []
        self._retry_seq = 0
        self._pump_task: asyncio.Task | None = None
        self.metrics = {"published": 0, "publish_rejected": 0, "retries_scheduled": 0,
                        "retry_overflow_deadlettered": 0, "retry_age_deadlettered": 0,
                        "dead_lettered": 0, "dead_letter_dropped_oldest": 0}

    def _get_queue(self, topic: str) -> asyncio.Queue:
        if topic not in self.topics:
            self.topics[topic] = asyncio.Queue(maxsize=self._queue_maxsize)
        return self.topics[topic]

    async def publish(self, topic: str, message: Any) -> None:
        queue = self._get_queue(topic)

        # Inject OTel context
        carrier = {}
        propagate.inject(carrier)

        payload = {
            "message": message,
            "carrier": carrier
        }

        # Natural backpressure: wait briefly for space, then REJECT — a full
        # bounded queue is a real capacity signal, not something to absorb.
        try:
            await asyncio.wait_for(queue.put(payload), timeout=self._put_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            self.metrics["publish_rejected"] += 1
            raise MessageBusFullError(
                f"Local queue for topic {topic!r} stayed full past "
                f"{self._put_timeout}s — publish rejected (§3.13)")

        self.metrics["published"] += 1
        msg_id = getattr(message, "task_id", getattr(message, "schedule_id", str(message)))
        if isinstance(message, dict):
            msg_id = message.get("task_id") or message.get("schedule_id", str(message))

        logger.info(f"Published to {topic}: {msg_id}")

    # ── Bounded delayed-retry machinery (§3.13) ───────────────────────

    def schedule_retry(self, topic: str, retry_payload: dict, delay: float) -> None:
        retry_payload["_topic"] = topic   # the pump routes by this
        if self._retry_age_limit and \
                time.time() - float(retry_payload.get("_retry_enqueued_at", time.time())) \
                > self._retry_age_limit:
            # Max retry age exceeded — straight to dead letters (bounded).
            asyncio.create_task(self._dead_letter(
                topic, retry_payload.get("message"),
                int(retry_payload.get("delivery_attempt", 1)),
                str(retry_payload.get("task_id") or retry_payload.get("schedule_id") or "retry-aged")))
            self.metrics["retry_age_deadlettered"] += 1
            return
        if len(self._retry_store) >= self._retry_store_limit:
            # Retry storage full (§3.13 "clear behavior when unavailable"):
            # the OLDEST pending retry is promoted to dead letters to make
            # room; consumers still never block.
            if self._retry_store:
                _, _, evicted = self._retry_store.pop(0)
                asyncio.create_task(self._dead_letter(
                    topic, evicted.get("message"),
                    int(evicted.get("delivery_attempt", 1)), "retry-store-overflow"))
            self.metrics["retry_overflow_deadlettered"] += 1
        self._retry_seq += 1
        self._retry_store.append((time.time() + max(0.0, delay), self._retry_seq, retry_payload))
        self.metrics["retries_scheduled"] += 1
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.get_running_loop().create_task(self._retry_pump())

    async def _retry_pump(self) -> None:
        """Single pump: moves DUE retries into their topic queue without ever
        blocking (a full queue keeps entries parked for the next tick), and
        dead-letters entries past the max retry age. Runs until cancelled —
        cancellation during requeue is safe by construction."""
        try:
            while True:
                await asyncio.sleep(0.25)
                if not self._retry_store:
                    continue
                now = time.time()
                due = [item for item in self._retry_store if item[0] <= now]
                if not due:
                    continue
                for item in sorted(due, key=lambda x: x[1]):
                    _, _, payload = item
                    if self._retry_age_limit and \
                            now - float(payload.get("_retry_enqueued_at", now)) \
                            > self._retry_age_limit:
                        self._retry_store.remove(item)
                        self.metrics["retry_age_deadlettered"] += 1
                        await self._dead_letter(
                            item_topic(payload), payload.get("message"),
                            int(payload.get("delivery_attempt", 1)), "retry-aged")
                        continue
                    queue = self._get_queue(item_topic(payload))
                    if queue.full():
                        continue   # stays parked; retried next tick
                    self._retry_store.remove(item)
                    queue.put_nowait(payload)
        except asyncio.CancelledError:
            raise

    async def _dead_letter(self, topic: str, message: Any, attempt: int,
                           message_id: str) -> None:
        entry = {"topic": topic, "message": message, "delivery_attempt": attempt,
                 "message_id": message_id}
        if self.dead_letters.full():
            # Bounded retention: drop the OLDEST (newest evidence wins).
            try:
                self.dead_letters.get_nowait()
                self.metrics["dead_letter_dropped_oldest"] += 1
            except asyncio.QueueEmpty:
                pass
        await self.dead_letters.put(entry)
        self.metrics["dead_lettered"] += 1

    async def consume(self, topic: str, handler: Callable[[TaskTriggerEvent, MessageContext], Awaitable[None]]) -> None:
        from backend.config.settings import settings
        queue = self._get_queue(topic)

        async def worker():
            while True:
                payload = await queue.get()
                ctx = InMemoryMessageContext(
                    str(id(payload)), queue, payload, self, topic,
                    max_deliveries=self.max_deliveries,
                    retry_base_seconds=self.retry_base_seconds,
                )
                token = otel_context.attach(propagate.extract(payload["carrier"]))
                try:
                    await handler(payload["message"], ctx)
                except asyncio.CancelledError:
                    if not ctx.settled:
                        await ctx.nack()
                    raise
                except Exception:
                    logger.exception("Error in consumer handler")
                    if not ctx.settled:
                        await ctx.nack()
                finally:
                    otel_context.detach(token)

        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(settings.LOCAL_WORKER_CONCURRENCY):
                    group.create_task(worker())
        finally:
            # Shutdown with pending messages (§3.13): the pump is cancelled
            # with the consumers — no orphaned requeues, no leaked task.
            if self._pump_task is not None and not self._pump_task.done():
                self._pump_task.cancel()


def item_topic(payload: dict) -> str:
    """The topic a retry belongs to. Retry payloads carry their topic so the
    pump can route them without a parallel map."""
    return payload.get("_topic", "agentos-workflow-events")
