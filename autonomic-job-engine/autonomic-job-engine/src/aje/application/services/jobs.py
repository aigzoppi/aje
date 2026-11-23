"""Job use cases: submit, feed, control and inspect farm and data-parallel jobs."""

from __future__ import annotations

from typing import Any

from aje.application import schemas
from aje.application.ports import backend as backend_lib
from aje.application.ports import repositories
from aje.application.runtime import compiler
from aje.application.runtime import data_parallel
from aje.application.runtime import engine as engine_lib
from aje.application.runtime import job
from aje.application.services import presenters
from aje.domain import errors


class JobService:
    """Use cases on jobs: submit, read, feed input, control and inspect."""

    def __init__(
        self,
        engine: engine_lib.AutonomicEngine,
        backend: backend_lib.Backend,
        jobs: repositories.JobRepository,
        tasks: repositories.TaskRepository,
        batches: repositories.BatchRepository,
        events: repositories.EventRepository,
    ):
        self.engine = engine
        self.backend = backend
        self.jobs = jobs
        self.tasks = tasks
        self.batches = batches
        self.events = events

    # ------------------------------------------------------------------ lookup helpers
    def live(self, job_id: str) -> job.JobManager:
        """The manager of a job that still accepts commands."""
        m = self.engine.managers.get(job_id)
        if m is not None:
            return m
        done = self.engine.finished.get(job_id)
        if done is not None:
            raise errors.ConflictError(f"job {job_id} is {done.status.value}")
        raise errors.NotFoundError(f"no active job {job_id}")

    def _data_parallel(self, job_id: str) -> data_parallel.DataParallelManager:
        m = self.live(job_id)
        if not isinstance(m, data_parallel.DataParallelManager):
            raise errors.InvalidOperationError("this operation is for data_parallel jobs")
        return m

    async def view(self, job_id: str) -> schemas.JobOut:
        row = await self.jobs.get(job_id)
        if row is None:
            raise errors.NotFoundError(f"no job {job_id}")
        return await self.present(row)

    async def present(self, row: repositories.JobRow) -> schemas.JobOut:
        """The API view of a stored job; counters always have the live counters' keys."""
        rebuilt = None
        if row.id not in self.engine.managers and row.counts is None:  # finished before counts were stored
            rebuilt = await self.jobs.counts(row.id, row.pattern)
        return presenters.job_out(row, self.engine, rebuilt)

    async def ensure_exists(self, job_id: str) -> None:
        if await self.jobs.get(job_id) is None:
            raise errors.NotFoundError(f"no job {job_id}")

    # ------------------------------------------------------------------ submit / query
    async def submit(self, spec: schemas.JobCreate, group_id: str | None = None) -> schemas.JobOut:
        m = await self.engine.create_job(spec, group_id=group_id)
        return await self.view(m.id)

    async def list(
        self,
        *,
        status: str | None = None,
        pattern: str | None = None,
        group_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[schemas.JobOut]:
        """Lists stored jobs, newest first, with live state where a manager exists.

        Args:
            status: Only jobs with this status.
            pattern: Only jobs of this pattern.
            group_id: Only members of this group.
            limit: Page size.
            offset: Rows to skip.

        Returns:
            The jobs.
        """
        rows = await self.jobs.list(
            status=status, pattern=pattern, group_id=group_id, limit=limit, offset=offset
        )
        return [await self.present(r) for r in rows]

    async def get(self, job_id: str) -> schemas.JobOut:
        return await self.view(job_id)

    async def cancel(self, job_id: str) -> schemas.JobOut:
        await self.live(job_id).cancel("api")
        return await self.view(job_id)

    # ------------------------------------------------------------------ input
    async def add_tasks(self, job_id: str, tasks: list[Any], close: bool) -> dict[str, Any]:
        m = self.live(job_id)
        if isinstance(m, data_parallel.DataParallelManager):
            raise errors.InvalidOperationError("data_parallel jobs take batches: POST .../batches")
        ids = await m.submit_tasks(tasks)
        if close:
            await m.close()
        return {"task_ids": ids, "closed": m.closed}

    async def list_tasks(
        self,
        job_id: str,
        *,
        status: str | None = None,
        include_payload: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[schemas.TaskOut]:
        await self.ensure_exists(job_id)
        rows = await self.tasks.list(job_id, status=status, limit=limit, offset=offset)
        return [presenters.task_out(r, include_payload) for r in rows]

    async def get_task(self, job_id: str, task_id: str) -> schemas.TaskOut:
        r = await self.tasks.get(task_id)
        if r is None or r.job_id != job_id:
            raise errors.NotFoundError("no such task")
        return presenters.task_out(r)

    async def add_batch(self, job_id: str, items: list[Any], partitions: int | None) -> dict[str, Any]:
        bid, parts = await self._data_parallel(job_id).submit_batch(items, partitions)
        return {"batch_id": bid, "partitions": parts}

    async def list_batches(
        self, job_id: str, *, include_result: bool = False, limit: int = 100, offset: int = 0
    ) -> list[schemas.BatchOut]:
        await self.ensure_exists(job_id)
        return [
            presenters.batch_out(r, include_result)
            for r in await self.batches.list(job_id, limit=limit, offset=offset)
        ]

    async def get_batch(self, job_id: str, batch_id: str) -> schemas.BatchOut:
        r = await self.batches.get(batch_id)
        if r is None or r.job_id != job_id:
            raise errors.NotFoundError("no such batch")
        return presenters.batch_out(r)

    async def close(self, job_id: str) -> schemas.JobOut:
        await self.live(job_id).close()
        return await self.view(job_id)

    # ------------------------------------------------------------------ control interface
    async def scale(self, job_id: str, workers: int) -> schemas.JobOut:
        await self.live(job_id).set_workers(workers, "api")
        return await self.view(job_id)

    async def set_partitions(self, job_id: str, partitions: int) -> schemas.JobOut:
        await self._data_parallel(job_id).set_partitions(partitions, "api")
        return await self.view(job_id)

    async def suspend(self, job_id: str) -> schemas.JobOut:
        await self.live(job_id).suspend("api")
        return await self.view(job_id)

    async def resume(self, job_id: str) -> schemas.JobOut:
        await self.live(job_id).resume("api")
        return await self.view(job_id)

    async def set_management(self, job_id: str, management: str) -> schemas.JobOut:
        await self.live(job_id).set_management(management)
        return await self.view(job_id)

    async def get_contract(self, job_id: str) -> dict[str, Any]:
        m = self.engine.managers.get(job_id)
        if m is not None:
            return presenters.contract_view(m.contract_info, include_source=True)
        row = await self.jobs.get(job_id)
        if row is None:
            raise errors.NotFoundError(f"no job {job_id}")
        return presenters.contract_view(row.contract, include_source=True)

    async def put_contract(self, job_id: str, spec: schemas.ContractSpec) -> dict[str, Any]:
        m = self.live(job_id)
        rulebase, info = compiler.compile_contract(m.pattern, spec)
        await m.set_contract(rulebase, info)
        return presenters.contract_view(info, include_source=True)

    # ------------------------------------------------------------------ monitoring
    def metrics(self, job_id: str) -> schemas.JobMetricsOut:
        m = self.live(job_id)
        return schemas.JobMetricsOut(
            job_id=m.id,
            workers=m.workers,
            backend=m.backend_status,
            escalations=list(m.escalations.values()),
            current=m.last_metrics,
            history=list(m.history),
        )

    def facts(self, job_id: str) -> dict[str, Any]:
        m = self.live(job_id)
        return m.facts(m.last_metrics or {})

    async def events_of(
        self, job_id: str, *, kind: str | None = None, after_id: int = 0, limit: int = 200
    ) -> list[schemas.EventOut]:
        rows = await self.events.list(job_id=job_id, kind=kind, after_id=after_id, limit=limit)
        return [presenters.event_out(r) for r in rows]

    async def workers(self, job_id: str) -> list[dict[str, Any]]:
        await self.ensure_exists(job_id)
        return await self.backend.workers(job_id)

    async def logs(self, job_id: str, worker: str | None, tail: int) -> dict[str, str]:
        await self.ensure_exists(job_id)
        return await self.backend.logs(job_id, worker, tail)
