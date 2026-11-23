"""Worker SDK of the Autonomic Job Engine.

    from aje_worker import run

    def handle(payload):
        return payload * payload

    if __name__ == "__main__":
        run(handle)

or, without writing a main: ``python -m aje_worker my_module:handle``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import os
import signal
import socket
import sys

from aje_worker import amqp
from aje_worker import protocol
from aje_worker.context import JobContext
from aje_worker.context import PostgresDataStore
from aje_worker.runtime import TaskContext
from aje_worker.runtime import Worker

__all__ = ["run", "Worker", "TaskContext", "JobContext", "PostgresDataStore"]


async def _main(
    handler: Callable,
    amqp_url: str,
    job_id: str,
    worker_id: str,
    heartbeat: float,
    grace: float,
    job_context: JobContext,
) -> int:
    """Runs one worker until it is drained or terminated; returns its exit code."""
    worker = Worker(
        handler,
        amqp.AmqpWorkerTransport(amqp_url, job_id),
        job_id=job_id,
        worker_id=worker_id,
        heartbeat_interval=heartbeat,
        term_grace=grace,
        job_context=job_context,
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_stop, protocol.EXIT_TERMINATED)
        except NotImplementedError:  # pragma: no cover - windows
            pass
    return await worker.run()


def run(
    handler: Callable, *, amqp_url: str | None = None, job_id: str | None = None, worker_id: str | None = None
) -> None:
    """Run a worker until drained (exit 0) or terminated (exit 143)."""
    logging.basicConfig(
        level=os.environ.get("AJE_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    amqp_url = amqp_url or os.environ.get(protocol.ENV_AMQP_URL, "amqp://guest:guest@localhost/")
    job_id = job_id or os.environ.get(protocol.ENV_JOB_ID)
    if not job_id:
        sys.exit(f"{protocol.ENV_JOB_ID} is not set")
    worker_id = worker_id or os.environ.get(protocol.ENV_WORKER_ID) or socket.gethostname()
    code = asyncio.run(
        _main(
            handler,
            amqp_url,
            job_id,
            worker_id,
            float(os.environ.get(protocol.ENV_HEARTBEAT, "5")),
            float(os.environ.get(protocol.ENV_TERM_GRACE, "20")),
            JobContext.from_env(),
        )
    )
    sys.exit(code)
