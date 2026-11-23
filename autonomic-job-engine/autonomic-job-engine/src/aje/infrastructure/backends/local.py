"""Runs workers as local OS processes with Kubernetes work-queue Job semantics.

Useful for docker-compose demos and development without a cluster:
  * ``workers`` processes are kept alive (crashed ones are replaced up to backoff_limit)
  * scale-down sends SIGTERM to the newest processes (exit 143, not a success)
  * once a worker exits 0 (drained) no replacements are started and the job completes
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import pathlib
import signal
import sys
import time
from typing import Any

import pydantic

from aje.application.ports import backend
import aje_worker
from aje_worker import protocol

# directory that contains the aje_worker package (src/ in a checkout, /app/aje in the API image),
# so local worker processes can run `python -m aje_worker ...` without an install
_WORKER_SDK_ROOT = str(pathlib.Path(aje_worker.__file__).resolve().parents[1])


class _Proc(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)

    name: str
    proc: asyncio.subprocess.Process
    log_path: str
    started: float
    terminating: bool = False


class _Job(pydantic.BaseModel):
    """A job's worker processes and the counters the backend reports."""

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)

    spec: backend.WorkerSpec
    env: dict[str, str]
    desired: int
    procs: list[_Proc] = pydantic.Field(default_factory=list)
    finished: collections.deque = pydantic.Field(default_factory=lambda: collections.deque(maxlen=50))
    suspended: bool = False
    succeeded: int = 0
    failed: int = 0
    completed: bool = False
    phase: str = "running"
    seq: int = 0
    lock: asyncio.Lock = pydantic.Field(default_factory=asyncio.Lock)


class LocalProcessBackend:
    """Runs workers as local processes, with Kubernetes Job semantics.

    Crashed workers are replaced until ``backoff_limit`` failures, exit code 0 completes
    the job, and each worker logs to a file under ``log_dir``.
    """

    name = "local"

    def __init__(self, amqp_url: str, log_dir: str = "/tmp/aje-workers", heartbeat: float = 5.0):
        self.amqp_url = amqp_url
        self.log_dir = pathlib.Path(log_dir)
        self.heartbeat = heartbeat
        self.jobs: dict[str, _Job] = {}
        self._reaper: asyncio.Task | None = None

    async def start(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._reaper = asyncio.create_task(self._reap_loop())

    async def close(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for job_id in list(self.jobs):
            await self.delete(job_id)

    # ------------------------------------------------------------------ api
    async def create(self, spec: backend.WorkerSpec, workers: int) -> None:
        """Starts ``workers`` processes running the job's command."""
        env = dict(os.environ)
        env.update(spec.env)
        env.update(
            {
                "AJE_AMQP_URL": self.amqp_url,
                protocol.ENV_JOB_ID: spec.job_id,
                "AJE_HEARTBEAT_INTERVAL": str(self.heartbeat),
                "PYTHONPATH": os.pathsep.join(filter(None, [_WORKER_SDK_ROOT, env.get("PYTHONPATH")])),
                "PYTHONUNBUFFERED": "1",
            }
        )
        if spec.job_context:  # local workers inherit the API environment, so the DSN variable is there too
            env[protocol.ENV_JOB_CONTEXT] = json.dumps(spec.job_context, separators=(",", ":"))
        self.jobs[spec.job_id] = _Job(spec=spec, env=env, desired=workers)
        await self._reconcile(self.jobs[spec.job_id])

    async def scale(self, job_id: str, workers: int) -> None:
        job = self.jobs[job_id]
        job.desired = workers
        await self._reconcile(job)

    async def suspend(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job.suspended = True
        job.phase = "suspended"
        await self._reconcile(job)

    async def resume(self, job_id: str, workers: int) -> None:
        job = self.jobs[job_id]
        job.suspended = False
        job.desired = workers
        job.phase = "running"
        await self._reconcile(job)

    async def delete(self, job_id: str) -> None:
        """Stops the job's processes (SIGTERM, then SIGKILL) and forgets the job."""
        job = self.jobs.pop(job_id, None)
        if not job:
            return
        for p in job.procs:
            if p.proc.returncode is None:
                p.proc.send_signal(signal.SIGTERM)
        deadline = time.time() + 10
        for p in job.procs:
            try:
                await asyncio.wait_for(p.proc.wait(), max(0.1, deadline - time.time()))
            except TimeoutError:
                p.proc.kill()

    async def status(self, job_id: str) -> backend.BackendStatus:
        job = self.jobs.get(job_id)
        if job is None:
            return backend.BackendStatus(phase="missing")
        alive = [p for p in job.procs if p.proc.returncode is None]
        return backend.BackendStatus(
            phase=job.phase, active=len(alive), ready=len(alive), succeeded=job.succeeded, failed=job.failed
        )

    async def workers(self, job_id: str) -> list[dict[str, Any]]:
        """Lists running and recently finished processes, with exit codes."""
        job = self.jobs.get(job_id)
        if job is None:
            return []
        out = [
            {
                "name": p.name,
                "pid": p.proc.pid,
                "state": "terminating" if p.terminating else "running",
                "started_at": p.started,
            }
            for p in job.procs
            if p.proc.returncode is None
        ]
        out += list(job.finished)
        return out

    async def logs(self, job_id: str, worker: str | None, tail: int) -> dict[str, str]:
        d = self.log_dir / job_id
        if not d.exists():
            return {}
        out = {}
        for f in sorted(d.glob("*.log")):
            if worker and f.stem != worker:
                continue
            lines = f.read_text(errors="replace").splitlines()[-tail:]
            out[f.stem] = "\n".join(lines)
        return out

    # ------------------------------------------------------------------ internals
    async def _spawn(self, job: _Job) -> None:
        """Starts one worker process with its own id and log file."""
        job.seq += 1
        name = f"{job.spec.job_name}-{job.seq}"
        env = dict(job.env)
        env[protocol.ENV_WORKER_ID] = name
        cmd = list(job.spec.command) + list(job.spec.args)
        if cmd and cmd[0] in ("python", "python3"):
            cmd[0] = sys.executable
        log_dir = self.log_dir / job.spec.job_id
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / f"{name}.log")
        with open(log_path, "ab") as fh:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=fh,
                stderr=asyncio.subprocess.STDOUT,
                cwd=_WORKER_SDK_ROOT,
                start_new_session=True,
            )
        job.procs.append(_Proc(name=name, proc=proc, log_path=log_path, started=time.time()))

    async def _reconcile(self, job: _Job) -> None:
        async with job.lock:
            alive = [p for p in job.procs if p.proc.returncode is None and not p.terminating]
            target = 0 if (job.suspended or job.completed or job.phase == "failed") else job.desired
            for _ in range(target - len(alive)):
                await self._spawn(job)
            if len(alive) > target:
                for p in sorted(alive, key=lambda p: p.started, reverse=True)[: len(alive) - target]:
                    p.terminating = True
                    p.proc.send_signal(signal.SIGTERM)

    async def _reap_loop(self) -> None:
        """Collects exited processes, counts them, replaces crashes and completes jobs."""
        while True:
            await asyncio.sleep(0.5)
            for job in list(self.jobs.values()):
                changed = False
                for p in list(job.procs):
                    rc = p.proc.returncode
                    if rc is None:
                        continue
                    job.procs.remove(p)
                    changed = True
                    job.finished.append(
                        {
                            "name": p.name,
                            "pid": p.proc.pid,
                            "state": "exited",
                            "exit_code": rc,
                            "started_at": p.started,
                        }
                    )
                    if rc == 0:
                        job.succeeded += 1
                        job.completed = True
                    elif p.terminating or rc in (protocol.EXIT_TERMINATED, -signal.SIGTERM):
                        pass  # deliberate scale-down / suspension
                    else:
                        job.failed += 1
                        if job.failed > job.spec.backoff_limit:
                            job.phase = "failed"
                if job.completed and not any(p.proc.returncode is None for p in job.procs):
                    if job.phase not in ("failed",):
                        job.phase = "complete"
                if changed:
                    await self._reconcile(job)
