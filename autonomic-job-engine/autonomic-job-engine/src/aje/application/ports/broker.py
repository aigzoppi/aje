"""Broker port: task and result queues per job, plus a control channel to its workers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

import pydantic


class QueueStats(pydantic.BaseModel):
    depth: int
    consumers: int


ResultCallback = Callable[[dict], Awaitable[None]]


class Subscription(Protocol):
    async def cancel(self) -> None: ...


class Broker(Protocol):
    """Engine-side view of the message broker.

    Per job: a task queue, a result queue and a control channel. Per output stream (an id
    independent of jobs): a durable queue the engine publishes accepted outputs to.
    """

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def declare_job(self, job_id: str) -> None: ...
    async def delete_job(self, job_id: str) -> None: ...
    async def publish_tasks(self, job_id: str, messages: list[dict]) -> None: ...
    async def queue_stats(self, job_id: str) -> QueueStats: ...
    async def consume_results(self, job_id: str, callback: ResultCallback) -> Subscription: ...
    async def broadcast_control(self, job_id: str, message: dict) -> None: ...
    async def purge_tasks(self, job_id: str) -> int: ...
    async def declare_output(self, output_id: str) -> None: ...
    async def publish_output(self, output_id: str, message: dict, message_id: str) -> None: ...
