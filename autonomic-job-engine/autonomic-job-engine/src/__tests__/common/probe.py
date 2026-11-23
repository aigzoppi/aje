"""EngineProbe: drive a running engine and observe what its managers did and persisted."""

from aje.application import schemas
from aje.application.runtime import engine as engine_lib
from aje.application.runtime import job as job_lib
from aje.infrastructure.persistence import db
from common import polling

JobRef = str | job_lib.JobManager


def _id(job: JobRef) -> str:
    return job if isinstance(job, str) else job.id


class EngineProbe:
    """Submits work to an engine, waits for outcomes and reads the rows it persisted."""

    def __init__(self, engine: engine_lib.AutonomicEngine):
        self.engine = engine

    # ---- drive
    async def submit(self, spec: schemas.JobCreate) -> job_lib.JobManager:
        return await self.engine.create_job(spec)

    async def submit_group(self, spec: schemas.GroupCreate):
        return await self.engine.create_group(spec)

    # ---- wait
    async def wait(self, condition: polling.Condition, timeout: float = 15.0):
        return await polling.wait_for(condition, timeout)

    async def reaches(
        self, job: job_lib.JobManager, status: schemas.JobStatus, timeout: float = 15.0
    ) -> job_lib.JobManager:
        await polling.wait_for(lambda: job.status == status, timeout)
        return job

    async def succeeded(self, job: job_lib.JobManager, timeout: float = 15.0) -> job_lib.JobManager:
        return await self.reaches(job, schemas.JobStatus.SUCCEEDED, timeout)

    # ---- read what was persisted
    async def tasks(self, job: JobRef) -> list[db.TaskRow]:
        return await self.engine.store.find(db.TaskRow, {"job_id": _id(job)})

    async def results(self, job: JobRef) -> list:
        """Results of the completed tasks, sorted."""
        return sorted(t.result for t in await self.tasks(job) if t.status == "done")

    async def batches(self, job: JobRef) -> list[db.BatchRow]:
        return await self.engine.store.find(db.BatchRow, {"job_id": _id(job)}, order_by="created_at")

    async def batch(self, batch_id: str) -> db.BatchRow | None:
        return await self.engine.store.get(db.BatchRow, batch_id)

    async def events(self, job: JobRef, kind: str | None = None) -> list[db.EventRow]:
        rows = await self.engine.store.find(db.EventRow, {"job_id": _id(job)}, order_by="id")
        return [r for r in rows if kind is None or r.kind == kind]
