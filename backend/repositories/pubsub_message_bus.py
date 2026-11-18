"""Google Cloud Pub/Sub transport for workflow and scheduler events.

The API publishes JSON messages and a long-running Cloud Run worker pool pulls
them from ``<topic>-worker-sub`` subscriptions. Pub/Sub provides at-least-once
delivery; workflow/task execution must therefore remain idempotent.
"""

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from backend.config.settings import settings
from backend.repositories.message_bus import MessageBus, MessageContext

logger = logging.getLogger(__name__)


class PubSubMessageContext(MessageContext):
    def __init__(self, message: Any):
        self.message = message
        self._settled = False

    async def ack(self) -> None:
        if not self._settled:
            self._settled = True
            self.message.ack()

    async def nack(self) -> None:
        if not self._settled:
            self._settled = True
            self.message.nack()


class PubSubMessageBus(MessageBus):
    """Non-blocking async adapter around the synchronous Pub/Sub client."""

    def __init__(self):
        if not settings.GOOGLE_CLOUD_PROJECT:
            raise ValueError("GOOGLE_CLOUD_PROJECT is required for Pub/Sub")
        from google.cloud import pubsub_v1

        self.publisher = pubsub_v1.PublisherClient()
        self.subscriber = pubsub_v1.SubscriberClient()
        self.project_id = settings.GOOGLE_CLOUD_PROJECT

    @staticmethod
    def _encode(message: Any) -> bytes:
        if hasattr(message, "model_dump"):
            message = message.model_dump(mode="json")
        elif hasattr(message, "to_dict"):
            message = message.to_dict()
        return json.dumps(message, separators=(",", ":"), default=str).encode("utf-8")

    async def publish(self, topic: str, message: Any) -> None:
        topic_path = self.publisher.topic_path(self.project_id, topic)
        payload = self._encode(message)
        future = self.publisher.publish(topic_path, payload)
        message_id = await asyncio.to_thread(future.result)
        logger.info("Published workflow event to %s (message_id=%s)", topic, message_id)

    async def consume(
        self,
        topic: str,
        handler: Callable[[Any, MessageContext], Awaitable[None]],
    ) -> None:
        """Pull messages and await each async handler on the worker event loop.

        Phase 3 §3.14: explicit subscriber flow control aligned with real
        worker capacity — the subscriber may hold at most
        PUBSUB_MAX_OUTSTANDING_MESSAGES unacknowledged messages (and
        PUBSUB_MAX_OUTSTANDING_BYTES). Flow control bounds what the
        transport delivers; tenant admission and the capacity leases decide
        whether ACCEPTED work may actually run. Each callback blocks its
        subscriber thread until the handler settles, so one in-flight
        message = one unit of outstanding work — delivery cannot race ahead
        of execution.
        """
        loop = asyncio.get_running_loop()
        subscription_path = self.subscriber.subscription_path(
            self.project_id, f"{topic}-worker-sub"
        )
        try:
            await asyncio.to_thread(
                self.subscriber.get_subscription,
                request={"subscription": subscription_path},
                timeout=10,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Required Pub/Sub pull subscription is missing or inaccessible: {subscription_path}"
            ) from exc

        async def process(message: Any) -> None:
            context = PubSubMessageContext(message)
            try:
                event = json.loads(message.data.decode("utf-8"))
                await handler(event, context)
                if not context._settled:
                    await context.ack()
            except Exception:
                logger.exception("Pub/Sub handler failed for topic %s", topic)
                if not context._settled:
                    # nack schedules redelivery after the ack deadline — a
                    # bounded defer, not a tight loop (§3.14).
                    await context.nack()

        def callback(message: Any) -> None:
            # Pub/Sub invokes callbacks on its own thread. Marshal the coroutine
            # onto this worker's running loop and let it settle the message.
            future = asyncio.run_coroutine_threadsafe(process(message), loop)
            try:
                future.result()
            except Exception:
                logger.exception("Could not schedule Pub/Sub callback for %s", topic)
                message.nack()

        from google.cloud import pubsub_v1
        flow_control = pubsub_v1.types.FlowControl(
            max_messages=settings.PUBSUB_MAX_OUTSTANDING_MESSAGES,
            max_bytes=settings.PUBSUB_MAX_OUTSTANDING_BYTES,
        )
        streaming_pull = self.subscriber.subscribe(
            subscription_path, callback=callback, flow_control=flow_control,
        )
        logger.info(
            "Worker pulling from %s (flow control: %d messages / %d bytes)",
            subscription_path, settings.PUBSUB_MAX_OUTSTANDING_MESSAGES,
            settings.PUBSUB_MAX_OUTSTANDING_BYTES,
        )
        try:
            await asyncio.Event().wait()
        finally:
            streaming_pull.cancel()
            try:
                await asyncio.to_thread(streaming_pull.result, timeout=10)
            except Exception:
                pass

    async def close(self) -> None:
        await asyncio.gather(
            asyncio.to_thread(self.publisher.transport.close),
            asyncio.to_thread(self.subscriber.close),
            return_exceptions=True,
        )
