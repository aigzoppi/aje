"""Workers as asyncio tasks inside the engine process. For tests and simulations.

``WorkerSpec.command[0]`` names a handler registered in ``handlers``. Workers talk to the
broker through the injected ``transports`` factory (job_id -> WorkerTransport), e.g.
``lambda job_id: AmqpWorkerTransport(url, job_id)``
to run real workers against RabbitMQ.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from aje.application.ports import backend
from aje_worker import context
from aje_worker import protocol
from aje_worker import runtime


async def _hard_stop(entry: dict, timeout: float = 5.0) -> None:
    """Stop a worker like a crash: nothing is acked, so in-flight tasks go back to the queue."""
    transport = entry["worker"].transport
    # stop the worker first, then drop its connection (bounded: never hang a shutdown)
    entry["task"].cancel()
    await asyncio.wait({entry["task"]}, timeout=timeout)
    try:
        await asyncio.wait_for(transport.close(), timeout)
    except Exception:  # pylint: disable=broad-exception-caught  # already closed or broken: either way it is gone
        pass


class InProcessBackend:
    """Runs workers as asyncio tasks in the engine's process (integration tests).

    Workers still talk to the broker through a real transport, so they behave like
    the worker image; only the process boundary is gone.
    """

    name = "inprocess"

    def __init__(
        self,
        transports: Callable[[str], runtime.WorkerTransport],
        handlers: dict[str, Callable],
        heartbeat: float = 0.2,
    ):
        self.transports = transports
        self.handlers = handlers
        self.heartbeat = heartbeat
        self.jobs: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        for job_id in list(self.jobs):
            await self.delete(job_id)

    async def create(self, spec: backend.WorkerSpec, workers: int) -> None:
        self.jobs[spec.job_id] = {
            "spec": spec,
            "desired": workers,
            "workers": [],
            "seq": 0,
            "suspended": False,
            "completed": False,
            "succeeded": 0,
            "failed": 0,
        }
        self._reconcile(spec.job_id)

    def _spawn(self, job_id: str) -> None:
        """Starts one more worker task for the job."""
        j = self.jobs[job_id]
        j["seq"] += 1
        spec: backend.WorkerSpec = j["spec"]
        handler = self.handlers[spec.command[0]]
        seq = j["seq"]
        name = f"{spec.job_name}-{seq}"
        w = runtime.Worker(
            handler,
            self.transports(job_id),
            job_id=job_id,
            worker_id=name,
            heartbeat_interval=self.heartbeat,
            term_grace=5,
            job_context=context.JobContext.model_validate(spec.job_context),
        )
        task = asyncio.create_task(w.run())
        entry = {"name": name, "worker": w, "task": task, "stopping": False}
        task.add_done_callback(lambda t, e=entry: self._on_exit(job_id, e, t))
        j["workers"].append(entry)

    def _on_exit(self, job_id: str, entry: dict, task: asyncio.Task) -> None:
        j = self.jobs.get(job_id)
        if j is None or entry not in j["workers"]:
            return
        j["workers"].remove(entry)
        code = None if task.cancelled() or task.exception() else task.result()
        if code == protocol.EXIT_DRAINED:
            j["succeeded"] += 1
            j["completed"] = True
        elif code != protocol.EXIT_TERMINATED:
            j["failed"] += 1
        self._reconcile(job_id)

    def _reconcile(self, job_id: str) -> None:
        j = self.jobs[job_id]
        alive = [e for e in j["workers"] if not e["stopping"]]
        target = 0 if j["suspended"] or j["completed"] else j["desired"]
        for _ in range(target - len(alive)):
            self._spawn(job_id)
        for e in sorted(alive, key=lambda e: e["name"], reverse=True)[: max(0, len(alive) - target)]:
            e["stopping"] = True
            e["worker"].request_stop(protocol.EXIT_TERMINATED)

    async def kill_one(self, job_id: str) -> str | None:
        """Simulate a crashed worker (in-flight task is redelivered)."""
        j = self.jobs[job_id]
        if not j["workers"]:
            return None
        e = j["workers"][0]
        await _hard_stop(e)
        return e["name"]

    async def scale(self, job_id: str, workers: int) -> None:
        self.jobs[job_id]["desired"] = workers
        self._reconcile(job_id)

    async def suspend(self, job_id: str) -> None:
        self.jobs[job_id]["suspended"] = True
        self._reconcile(job_id)

    async def resume(self, job_id: str, workers: int) -> None:
        j = self.jobs[job_id]
        j["suspended"] = False
        j["desired"] = workers
        self._reconcile(job_id)

    async def delete(self, job_id: str) -> None:
        j = self.jobs.pop(job_id, None)
        if j:
            for e in j["workers"]:
                await _hard_stop(e)

    async def status(self, job_id: str) -> backend.BackendStatus:
        j = self.jobs.get(job_id)
        if j is None:
            return backend.BackendStatus(phase="missing")
        n = len(j["workers"])
        if j["completed"] and n == 0:
            phase = "complete"
        elif j["suspended"]:
            phase = "suspended"
        else:
            phase = "running"
        return backend.BackendStatus(
            phase=phase, active=n, ready=n, succeeded=j["succeeded"], failed=j["failed"]
        )

    async def workers(self, job_id: str) -> list[dict[str, Any]]:
        j = self.jobs.get(job_id) or {"workers": []}
        return [
            {"name": e["name"], "state": "terminating" if e["stopping"] else "running"} for e in j["workers"]
        ]

    async def logs(self, job_id: str, worker: str | None, tail: int) -> dict[str, str]:
        del job_id, worker, tail  # Unused: in-process workers log to the engine's log.
        return {}
