"""RabbitMQ (classic queues) transport for workers, based on aio-pika."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import aio_pika
from aio_pika import abc

from aje_worker import protocol


class AmqpWorkerTransport:
    """Worker side of the wire protocol on RabbitMQ: tasks in, results and heartbeats out."""

    def __init__(self, url: str, job_id: str):
        self.url = url
        self.job_id = job_id
        self._conn: aio_pika.abc.AbstractRobustConnection | None = None
        self._ch = None
        self._tasks = None
        self._tag: str | None = None

    async def connect(self) -> None:
        self._conn = await aio_pika.connect_robust(self.url)
        self._ch = await self._conn.channel()
        await self._ch.set_qos(prefetch_count=1)
        self._tasks = await self._ch.declare_queue(protocol.tasks_queue(self.job_id), durable=True)
        await self._ch.declare_queue(protocol.results_queue(self.job_id), durable=True)

    async def consume_tasks(self, callback: Callable) -> None:
        async def on_message(message: abc.AbstractIncomingMessage) -> None:
            try:
                body = protocol.unpack(message.body)
            except ValueError:
                await message.reject(requeue=False)
                return
            await callback(body, message.ack, lambda: message.nack(requeue=True))

        self._tag = await self._tasks.consume(on_message, no_ack=False)

    async def stop_consuming(self) -> None:
        if self._tag and self._tasks is not None:
            try:
                await self._tasks.cancel(self._tag)
            except Exception:  # pylint: disable=broad-exception-caught  # channel may already be closing
                pass
            self._tag = None

    async def publish_result(self, message: dict) -> None:
        await self._ch.default_exchange.publish(
            aio_pika.Message(
                protocol.pack(message),
                content_type=protocol.CONTENT_TYPE,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=protocol.results_queue(self.job_id),
        )

    async def subscribe_control(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        """Delivers control messages (e.g. drain) broadcast to every worker of the job."""
        exchange = await self._ch.declare_exchange(
            protocol.control_exchange(self.job_id), aio_pika.ExchangeType.FANOUT, durable=True
        )
        queue = await self._ch.declare_queue(exclusive=True, auto_delete=True)
        await queue.bind(exchange)

        async def on_control(message: abc.AbstractIncomingMessage) -> None:
            try:
                await callback(protocol.unpack(message.body))
            except ValueError:
                pass

        await queue.consume(on_control, no_ack=True)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
