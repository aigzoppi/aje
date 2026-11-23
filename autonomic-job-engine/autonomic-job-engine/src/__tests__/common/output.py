"""OutputReader: consume an output stream from RabbitMQ the way a downstream service would."""

import asyncio
from collections.abc import Callable
from typing import Any

import aio_pika
import pydantic

from aje_worker import protocol


class Output(pydantic.BaseModel):
    """One message of an output stream: the decoded body and the AMQP properties consumers see."""

    body: dict[str, Any]
    message_id: str | None
    content_type: str | None
    type: str | None
    headers: dict[str, Any]


class OutputReader:
    """Reads ``aje.output.<id>`` (declaring it, like a consumer that starts first)."""

    def __init__(self, amqp_url: str, output_id: str):
        self.amqp_url = amqp_url
        self.output_id = output_id
        self._conn: aio_pika.abc.AbstractRobustConnection | None = None
        self._queue: aio_pika.abc.AbstractQueue | None = None

    async def __aenter__(self) -> "OutputReader":
        self._conn = await aio_pika.connect_robust(self.amqp_url)
        channel = await self._conn.channel()
        self._queue = await channel.declare_queue(protocol.output_queue(self.output_id), durable=True)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._conn.close()

    async def read_until(self, done: Callable[[list[Output]], bool], timeout: float = 20.0) -> list[Output]:
        """Reads messages until ``done(messages)`` holds.

        Raises:
            AssertionError: ``done`` still fails after ``timeout`` seconds.
        """
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        out: list[Output] = []
        while not done(out):
            message = await self._queue.get(no_ack=True, fail=False)
            if message is None:
                if loop.time() > end:
                    raise AssertionError(f"output stream {self.output_id}: condition not met, got {out}")
                await asyncio.sleep(0.05)
                continue
            out.append(
                Output(
                    body=protocol.unpack(message.body),
                    message_id=message.message_id,
                    content_type=message.content_type,
                    type=message.type,
                    headers=dict(message.headers or {}),
                )
            )
        return out


def finished(*job_ids: str) -> Callable[[list[Output]], bool]:
    """Condition: every listed job has published its ``job.finished`` message."""

    def check(messages: list[Output]) -> bool:
        ended = {m.body["job_id"] for m in messages if m.body["kind"] == "job.finished"}
        return set(job_ids) <= ended

    return check
