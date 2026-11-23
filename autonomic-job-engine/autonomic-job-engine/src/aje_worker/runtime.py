"""Transport-independent worker loop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import inspect
import json
import logging
import time
from typing import Any, Protocol

import pydantic

from aje_worker import context
from aje_worker import protocol

log = logging.getLogger("aje.worker")

Ack = Callable[[], Awaitable[None]]


class WorkerTransport(Protocol):
    async def connect(self) -> None: ...
    async def consume_tasks(self, callback: Callable[[dict, Ack, Ack], Awaitable[None]]) -> None: ...
    async def stop_consuming(self) -> None: ...
    async def publish_result(self, message: dict) -> None: ...
    async def subscribe_control(self, callback: Callable[[dict], Awaitable[None]]) -> None: ...
    async def close(self) -> None: ...


class TaskContext(pydantic.BaseModel):
    """What a handler knows about the task it runs (second handler argument)."""

    job_id: str
    task_id: str
    attempt: int
    worker_id: str
    batch_id: str | None = None
    partition: int | None = None
    job_context: context.JobContext = pydantic.Field(
        default_factory=context.JobContext, description="the job's jobcontext"
    )


def _wants_context(handler: Callable) -> bool:
    try:
        params = [
            p
            for p in inspect.signature(handler).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        return len(params) >= 2
    except (TypeError, ValueError):
        return False


class Worker:
    """Pulls tasks one at a time, runs the handler, publishes results.

    ``handler(payload)`` or ``handler(payload, ctx)``; sync handlers run in a
    thread so AMQP heartbeats keep flowing during long computations.
    """

    def __init__(
        self,
        handler: Callable,
        transport: WorkerTransport,
        *,
        job_id: str,
        worker_id: str,
        heartbeat_interval: float = 5.0,
        term_grace: float = 20.0,
        job_context: context.JobContext | None = None,
    ):
        self.handler = handler
        self.job_context = job_context or context.JobContext()
        self.transport = transport
        self.job_id = job_id
        self.worker_id = worker_id
        self.heartbeat_interval = heartbeat_interval
        self.term_grace = term_grace
        self._ctx = _wants_context(handler)
        self._async = inspect.iscoroutinefunction(handler)
        self._stop = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._busy = False
        self.exit_code = protocol.EXIT_DRAINED
        self.processed = 0

    def request_stop(self, exit_code: int = protocol.EXIT_TERMINATED) -> None:
        if not self._stop.is_set():
            self.exit_code = exit_code
            self._stop.set()

    async def run(self) -> int:
        """Consumes tasks until drained or stopped, then leaves cleanly.

        Returns:
            The exit code: drained (0) or terminated.
        """
        await self.transport.connect()
        await self.transport.subscribe_control(self._on_control)
        await self.transport.consume_tasks(self._on_task)
        hb = asyncio.create_task(self._heartbeat())
        log.info("worker %s consuming job %s", self.worker_id, self.job_id)
        try:
            await self._stop.wait()
            await self.transport.stop_consuming()
            if self._busy:
                try:
                    await asyncio.wait_for(self._idle.wait(), self.term_grace)
                except TimeoutError:
                    log.warning(
                        "task still running after %.0fs grace; it will be redelivered", self.term_grace
                    )
        finally:
            hb.cancel()
            await self._send_heartbeat(busy=False, leaving=True)
            await self.transport.close()
        log.info("worker %s exiting with %s after %d task(s)", self.worker_id, self.exit_code, self.processed)
        return self.exit_code

    async def _on_control(self, message: dict) -> None:
        if message.get("cmd") == "drain":
            self.request_stop(protocol.EXIT_DRAINED)

    async def _on_task(self, message: dict, ack: Ack, requeue: Ack) -> None:
        """Runs the handler on one task and publishes the result (or the error), then acks."""
        if self._stop.is_set():
            await requeue()
            return
        self._busy = True
        self._idle.clear()
        started = time.time()
        ctx = TaskContext(
            job_id=self.job_id,
            task_id=message.get("task_id", ""),
            attempt=int(message.get("attempt", 1)),
            worker_id=self.worker_id,
            batch_id=message.get("batch_id"),
            partition=message.get("partition"),
            job_context=self.job_context,
        )
        result: dict[str, Any] = {
            "type": "result",
            "job_id": self.job_id,
            "task_id": message.get("task_id"),
            "attempt": ctx.attempt,
            "batch_id": ctx.batch_id,
            "partition": ctx.partition,
            "worker": self.worker_id,
            "started_at": started,
        }
        try:
            value = await self._call(message.get("payload"), ctx)
            json.dumps(value)  # results must be JSON serialisable
            result.update(ok=True, result=value)
        except Exception as e:  # pylint: disable=broad-exception-caught  # every failure is reported to the engine
            result.update(ok=False, error=f"{type(e).__name__}: {e}")
        result["finished_at"] = time.time()
        try:
            await self.transport.publish_result(result)
            await ack()
            self.processed += 1
        finally:
            self._busy = False
            self._idle.set()

    async def _call(self, payload: Any, ctx: TaskContext) -> Any:
        args = (payload, ctx) if self._ctx else (payload,)
        if self._async:
            return await self.handler(*args)
        return await asyncio.to_thread(self.handler, *args)

    async def _heartbeat(self) -> None:
        while True:
            await self._send_heartbeat(self._busy)
            await asyncio.sleep(self.heartbeat_interval)

    async def _send_heartbeat(self, busy: bool, leaving: bool = False) -> None:
        """Tells the engine the worker is alive (and whether it is busy, or leaving)."""
        try:
            await self.transport.publish_result(
                {
                    "type": "heartbeat",
                    "job_id": self.job_id,
                    "worker": self.worker_id,
                    "busy": busy,
                    "leaving": leaving,
                    "ts": time.time(),
                }
            )
        except Exception:  # pylint: disable=broad-exception-caught  # heartbeats are best effort
            pass
