"""RabbitMQ broker (classic durable queues) for the engine."""

from __future__ import annotations

import asyncio
import logging

import aio_pika
from aio_pika import abc

from aje.application.ports import broker
from aje_worker import protocol

log = logging.getLogger("aje.broker")

PUBLISH_ATTEMPTS = 3
_RETRYABLE = (aio_pika.exceptions.AMQPError, ConnectionError, TimeoutError)


class _Sub:
    """A result-queue subscription; ``cancel`` stops it and closes its channel."""

    def __init__(self, queue, tag, channel):
        self.queue, self.tag, self.channel = queue, tag, channel

    async def cancel(self) -> None:
        try:
            await self.queue.cancel(self.tag)
            await self.channel.close()
        except Exception:  # pylint: disable=broad-exception-caught  # cancelling is best effort: the channel may be gone
            pass


class AmqpBroker:
    """The broker port on RabbitMQ (aio-pika, robust connection)."""

    def __init__(self, url: str):
        self.url = url
        self._conn: aio_pika.abc.AbstractRobustConnection | None = None
        self._pub = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aio_pika.connect_robust(self.url)
        self._pub = await self._conn.channel(publisher_confirms=True)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def declare_job(self, job_id: str) -> None:
        ch = self._pub
        await ch.declare_queue(protocol.tasks_queue(job_id), durable=True)
        await ch.declare_queue(protocol.results_queue(job_id), durable=True)
        await ch.declare_exchange(
            protocol.control_exchange(job_id), aio_pika.ExchangeType.FANOUT, durable=True
        )

    async def delete_job(self, job_id: str) -> None:
        """Deletes the job's queues and control exchange (missing ones are skipped)."""
        ch = await self._conn.channel()
        try:
            for q in (protocol.tasks_queue(job_id), protocol.results_queue(job_id)):
                try:
                    await ch.queue_delete(q)
                except Exception:  # pylint: disable=broad-exception-caught  # the queue may not exist; the channel is reopened
                    ch = await self._conn.channel()
            try:
                await ch.exchange_delete(protocol.control_exchange(job_id))
            except Exception:  # pylint: disable=broad-exception-caught  # the exchange may not exist
                pass
        finally:
            try:
                await ch.close()
            except Exception:  # pylint: disable=broad-exception-caught  # closing is best effort
                pass

    async def publish_tasks(self, job_id: str, messages: list[dict]) -> None:
        # Publish the whole batch, then wait for all confirms: one round trip instead of one per message.
        exchange, key = self._pub.default_exchange, protocol.tasks_queue(job_id)
        async with self._lock:
            for start in range(0, len(messages), 500):
                await asyncio.gather(
                    *(
                        exchange.publish(
                            aio_pika.Message(
                                protocol.pack(m),
                                content_type=protocol.CONTENT_TYPE,
                                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                            ),
                            routing_key=key,
                        )
                        for m in messages[start : start + 500]
                    )
                )

    async def queue_stats(self, job_id: str) -> broker.QueueStats:
        """Depth and consumer count of the task queue (zeros when it does not exist)."""
        ch = await self._conn.channel()
        try:
            q = await ch.declare_queue(protocol.tasks_queue(job_id), passive=True)
            r = q.declaration_result
            return broker.QueueStats(depth=r.message_count, consumers=r.consumer_count)
        except Exception:  # pylint: disable=broad-exception-caught  # a missing queue reads as empty
            return broker.QueueStats(depth=0, consumers=0)
        finally:
            try:
                await ch.close()
            except Exception:  # pylint: disable=broad-exception-caught  # closing is best effort
                pass

    async def purge_tasks(self, job_id: str) -> int:
        ch = await self._conn.channel()
        try:
            q = await ch.declare_queue(protocol.tasks_queue(job_id), durable=True)
            r = await q.purge()
            return getattr(r, "message_count", 0) or 0
        finally:
            await ch.close()

    async def consume_results(self, job_id: str, callback: broker.ResultCallback) -> _Sub:
        """Delivers every result and heartbeat of the job to ``callback``, then acks it.

        Args:
            job_id: The job.
            callback: Receives each decoded message. A failing callback is logged and the
                message is still acked, so a bug cannot block the queue.

        Returns:
            The subscription.
        """
        ch = await self._conn.channel()
        await ch.set_qos(prefetch_count=200)
        q = await ch.declare_queue(protocol.results_queue(job_id), durable=True)

        async def on_message(message: abc.AbstractIncomingMessage) -> None:
            try:
                body = protocol.unpack(message.body)
            except ValueError:
                await message.reject(requeue=False)
                return
            try:
                await callback(body)
            except Exception:  # pylint: disable=broad-exception-caught  # never lose a result because of a bug
                log.exception("result handler failed for job %s", job_id)
            await message.ack()

        tag = await q.consume(on_message, no_ack=False)
        return _Sub(q, tag, ch)

    async def broadcast_control(self, job_id: str, message: dict) -> None:
        ch = self._pub
        ex = await ch.declare_exchange(
            protocol.control_exchange(job_id), aio_pika.ExchangeType.FANOUT, durable=True
        )
        await ex.publish(
            aio_pika.Message(protocol.pack(message), content_type=protocol.CONTENT_TYPE), routing_key=""
        )

    async def declare_output(self, output_id: str) -> None:
        await self._pub.declare_queue(protocol.output_queue(output_id), durable=True)

    async def publish_output(self, output_id: str, message: dict, message_id: str) -> None:
        """Publishes one output message: msgpack, persistent, confirmed by the broker.

        Transient failures (a reconnecting channel, an unroutable message because the
        queue was deleted) are retried, re-declaring the queue, with a short backoff.

        Args:
            output_id: The output stream.
            message: The output, with at least ``kind`` and ``job_id``.
            message_id: Idempotency key for consumers (stable across redeliveries).

        Raises:
            aio_pika.exceptions.AMQPError: The broker still refuses after the retries.
        """
        queue = protocol.output_queue(output_id)
        amqp_message = aio_pika.Message(
            protocol.pack(message),
            content_type=protocol.CONTENT_TYPE,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=message_id,
            type=message["kind"],
            app_id="aje",
            headers={"job_id": message["job_id"], "kind": message["kind"]},
        )
        for attempt in range(1, PUBLISH_ATTEMPTS + 1):
            try:
                await self._pub.default_exchange.publish(amqp_message, routing_key=queue)
                return
            except _RETRYABLE:
                if attempt == PUBLISH_ATTEMPTS:
                    raise
                log.warning("publishing to %s failed (attempt %d), retrying", queue, attempt)
                await asyncio.sleep(0.2 * 2**attempt)
                await self.declare_output(output_id)
