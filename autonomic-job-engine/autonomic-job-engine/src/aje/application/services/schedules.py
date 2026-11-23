"""Schedule use cases: cron schedules that launch jobs from a template."""

from __future__ import annotations

from aje.application import schemas
from aje.application.ports import repositories
from aje.application.runtime import engine as engine_lib
from aje.application.runtime import scheduler as scheduler_lib
from aje.application.services import presenters
from aje.domain import errors


class ScheduleService:
    """Use cases on cron schedules: create, read, change, trigger, list runs."""

    def __init__(
        self,
        engine: engine_lib.AutonomicEngine,
        scheduler: scheduler_lib.Scheduler,
        runs: repositories.ScheduleRunRepository,
        jobs: repositories.JobRepository,
    ):
        self.engine = engine
        self.scheduler = scheduler
        self.run_repo = runs
        self.jobs = jobs

    def _out(self, schedule_id: str) -> schemas.ScheduleOut:
        row = self.scheduler.get(schedule_id)
        return presenters.schedule_out(row, self.scheduler.active_jobs(row.id))

    async def _runs_out(self, runs) -> list[schemas.ScheduleRunOut]:
        stored = await self.jobs.statuses([r.job_id for r in runs if r.job_id])

        def job_status(jid: str | None) -> str | None:
            if jid is None:
                return None
            m = self.engine.job(jid)
            return m.status.value if m else stored.get(jid)

        return [presenters.schedule_run_out(r, job_status(r.job_id)) for r in runs]

    async def create(self, spec: schemas.ScheduleCreate) -> schemas.ScheduleOut:
        row = await self.scheduler.create(spec)
        return self._out(row.id)

    def list(self, *, status: str | None = None, group_id: str | None = None) -> list[schemas.ScheduleOut]:
        rows = sorted(self.scheduler.schedules.values(), key=lambda r: r.created_at)
        return [
            presenters.schedule_out(r, self.scheduler.active_jobs(r.id))
            for r in rows
            if (status is None or r.status == status) and (group_id is None or r.group_id == group_id)
        ]

    def get(self, schedule_id: str) -> schemas.ScheduleOut:
        return self._out(schedule_id)

    async def update(self, schedule_id: str, patch: schemas.ScheduleUpdate) -> schemas.ScheduleOut:
        await self.scheduler.update(schedule_id, patch)
        return self._out(schedule_id)

    async def delete(self, schedule_id: str, cancel_active: bool) -> dict:
        cancelled = await self.scheduler.delete(schedule_id, cancel_active)
        return {"id": schedule_id, "deleted": True, "cancelled_jobs": cancelled}

    async def suspend(self, schedule_id: str) -> schemas.ScheduleOut:
        await self.scheduler.suspend(schedule_id)
        return self._out(schedule_id)

    async def resume(self, schedule_id: str) -> schemas.ScheduleOut:
        await self.scheduler.resume(schedule_id)
        return self._out(schedule_id)

    async def trigger(self, schedule_id: str) -> schemas.ScheduleRunOut:
        run = await self.scheduler.trigger(schedule_id)
        if run.status == "skipped":
            raise errors.ConflictError(run.message)
        if run.status == "failed":
            raise errors.InvalidOperationError(run.message)
        return (await self._runs_out([run]))[0]

    async def runs(
        self, schedule_id: str, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[schemas.ScheduleRunOut]:
        self.scheduler.get(schedule_id)
        return await self._runs_out(
            await self.run_repo.list(schedule_id, status=status, limit=limit, offset=offset)
        )
