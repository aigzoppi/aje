"""Cron schedules: launch a job from a template every time a cron expression fires.

The engine owns one scheduler task (consistent with the single API replica design):
  * each active schedule keeps its next fire time in the database
  * the loop sleeps until the earliest one (at most 60 s, or until a schedule changes)
  * ``concurrency_policy`` decides what happens if the previous run is still active:
    ``allow`` starts another, ``forbid`` skips the run, ``replace`` cancels the old run first
  * after a restart, a run missed while the engine was down is launched once
    (only if it is at most ``starting_deadline_seconds`` late, when that is set)

Every run, started or not, is recorded in ``schedule_runs``.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, TYPE_CHECKING

from aje.application import schemas
from aje.application.runtime import compiler
from aje.application.runtime import job as job_lib
from aje.domain import cron as cron_lib
from aje.domain import errors
from aje.infrastructure.persistence import db

if TYPE_CHECKING:
    from aje.application.runtime import engine as engine_lib

log = logging.getLogger("aje.scheduler")

LABEL_SCHEDULE = "aje.io/schedule"
MAX_SLEEP = 60.0


def run_name(schedule_name: str, at: datetime.datetime) -> str:
    """Kubernetes CronJob convention: <name>-<unix minutes> (fits the 40-char job name)."""
    return f"{schedule_name}-{int(cron_lib.as_utc(at).timestamp()) // 60}"


class ScheduleNotFoundError(errors.NotFoundError):
    """No schedule has this id."""


class Scheduler:
    """Fires jobs from cron schedules (see the module docstring for the semantics).

    Attributes:
        schedules: Every schedule by id, kept in memory and written through to the database.
    """

    def __init__(self, engine: engine_lib.AutonomicEngine):
        self.engine = engine
        self.schedules: dict[str, db.ScheduleRow] = {}
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Load schedules and launch catch-up runs. Call after the jobs are recovered."""
        now = db.utcnow()
        for row in await self.engine.store.find(db.ScheduleRow):
            self.schedules[row.id] = row
        async with self._lock:
            for row in self.schedules.values():
                if row.status == "active":
                    await self._catch_up(row, now)
        if self.schedules:
            log.info("loaded %d schedule(s)", len(self.schedules))
        self._task = asyncio.create_task(self._loop(), name="aje-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _catch_up(self, row: db.ScheduleRow, now: datetime.datetime) -> None:
        """Fires (or records as skipped) a run missed while the engine was down, then reschedules."""
        due = cron_lib.as_utc(row.next_run_at) if row.next_run_at else None
        if due is not None and due <= now:
            late = (now - due).total_seconds()
            if row.starting_deadline_seconds is None or late <= row.starting_deadline_seconds:
                await self._fire(row, due, "catch-up")
            else:
                await self._record(
                    row,
                    due,
                    "catch-up",
                    "skipped",
                    None,
                    f"missed by {late:.0f}s (starting deadline {row.starting_deadline_seconds}s)",
                )
        await self._save(row, next_run_at=cron_lib.next_fire(row.cron, row.timezone, now))

    async def _loop(self) -> None:
        """Ticks, then sleeps until the next fire time or until a schedule changes."""
        while True:
            try:
                await self._tick()
            except Exception:  # pylint: disable=broad-exception-caught  # the scheduler must survive any single failure
                log.exception("scheduler tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self._sleep_for())
            except TimeoutError:
                pass

    def _sleep_for(self) -> float:
        now = db.utcnow()
        due = [
            cron_lib.as_utc(r.next_run_at)
            for r in self.schedules.values()
            if r.status == "active" and r.next_run_at is not None
        ]
        if not due:
            return MAX_SLEEP
        return min(MAX_SLEEP, max(0.05, (min(due) - now).total_seconds()))

    async def _tick(self) -> None:
        """Fires every active schedule that is due; missed fire times collapse into one run."""
        async with self._lock:
            now = db.utcnow()
            for row in list(self.schedules.values()):
                if (
                    row.status != "active"
                    or row.next_run_at is None
                    or cron_lib.as_utc(row.next_run_at) > now
                ):
                    continue
                try:
                    await self._fire(row, cron_lib.as_utc(row.next_run_at), "cron")
                except Exception:  # pylint: disable=broad-exception-caught  # one schedule failing must not stop the others
                    log.exception("schedule %s could not fire", row.id)
                # several missed fire times collapse into this single run
                await self._save(row, next_run_at=cron_lib.next_fire(row.cron, row.timezone, now))

    # ------------------------------------------------------------------ firing
    def active_jobs(self, schedule_id: str) -> list[str]:
        return [
            m.id
            for m in self.engine.managers.values()
            if m.spec.labels.get(LABEL_SCHEDULE) == schedule_id and m.status not in schemas.TERMINAL
        ]

    async def _fire(
        self, row: db.ScheduleRow, scheduled_at: datetime.datetime, trigger: str
    ) -> db.ScheduleRunRow:
        """Starts a run, applying the concurrency policy, and records it.

        Args:
            row: The schedule.
            scheduled_at: The fire time this run is for.
            trigger: ``cron``, ``catch-up`` or ``manual``.

        Returns:
            The recorded run: started, skipped (``forbid`` with an active run) or failed.
        """
        active = self.active_jobs(row.id)
        if active and row.concurrency_policy == "forbid":
            still_active = ", ".join(active)
            return await self._record(
                row, scheduled_at, trigger, "skipped", None, f"previous run still active: {still_active}"
            )
        if active and row.concurrency_policy == "replace":
            for jid in active:
                m = self.engine.managers.get(jid)
                if m is not None:
                    try:
                        await m.cancel("schedule")
                    except job_lib.JobStateError:
                        pass  # finished in the meantime
        template = schemas.JobTemplate.model_validate(row.job)
        spec = schemas.JobCreate(**template.model_dump(), name=run_name(row.name, scheduled_at))
        spec.labels = {**spec.labels, LABEL_SCHEDULE: row.id}
        try:
            m = await self.engine.create_job(spec, group_id=row.group_id)
        except Exception as e:  # pylint: disable=broad-exception-caught  # recorded on the run, the schedule keeps going
            return await self._record(row, scheduled_at, trigger, "failed", None, str(e))
        run = await self._record(row, scheduled_at, trigger, "started", m.id, None)
        await self._save(row, last_run_at=scheduled_at, last_job_id=m.id)  # after the run is visible
        return run

    async def _record(
        self,
        row: db.ScheduleRow,
        scheduled_at: datetime.datetime,
        trigger: str,
        status: str,
        job_id: str | None,
        message: str | None,
    ) -> db.ScheduleRunRow:
        """Persists a run and announces it as a ``schedule.<status>`` event."""
        run = db.ScheduleRunRow(
            schedule_id=row.id,
            scheduled_at=scheduled_at,
            created_at=db.utcnow(),
            trigger=trigger,
            status=status,
            job_id=job_id,
            message=message,
        )
        await self.engine.store.add(run)
        text = {
            "started": f"schedule {row.name!r} started job {job_id}",
            "skipped": f"schedule {row.name!r} skipped a run: {message}",
            "failed": f"schedule {row.name!r} could not start a job: {message}",
        }[status]
        await self.engine.bus.emit(
            f"schedule.{status}",
            text,
            job_id=job_id,
            group_id=row.group_id,
            data={"schedule_id": row.id, "trigger": trigger, "scheduled_at": scheduled_at.isoformat()},
        )
        return run

    async def _save(self, row: db.ScheduleRow, **values: Any) -> None:
        values["updated_at"] = db.utcnow()
        for k, v in values.items():
            setattr(row, k, v)
        await self.engine.store.update(db.ScheduleRow, row.id, **values)

    # ------------------------------------------------------------------ API operations
    def get(self, schedule_id: str) -> db.ScheduleRow:
        row = self.schedules.get(schedule_id)
        if row is None:
            raise ScheduleNotFoundError(f"no schedule {schedule_id}")
        return row

    def _validate(self, job: schemas.JobTemplate, group_id: str | None) -> None:
        compiler.compile_contract(job.pattern, job.contract)  # fail now, not at 2 a.m.
        if group_id is not None and group_id not in self.engine.groups:
            raise ValueError(f"no active group {group_id}")

    async def create(self, spec: schemas.ScheduleCreate) -> db.ScheduleRow:
        """Creates a schedule.

        Args:
            spec: The schedule, with the template of the jobs it starts.

        Returns:
            The stored schedule.

        Raises:
            ValueError: The name is taken or the group does not exist.
            dsl.ContractError: The job template's contract is invalid.
        """
        self._validate(spec.job, spec.group_id)
        async with self._lock:
            if any(r.name == spec.name for r in self.schedules.values()):
                raise ValueError(f"a schedule named {spec.name!r} already exists")
            now = db.utcnow()
            row = db.ScheduleRow(
                id=job_lib.new_id(),
                name=spec.name,
                cron=spec.cron,
                timezone=spec.timezone,
                status="suspended" if spec.suspended else "active",
                concurrency_policy=spec.concurrency_policy,
                starting_deadline_seconds=spec.starting_deadline_seconds,
                group_id=spec.group_id,
                job=spec.job.model_dump(mode="json"),
                next_run_at=None if spec.suspended else cron_lib.next_fire(spec.cron, spec.timezone, now),
                created_at=now,
                updated_at=now,
            )
            await self.engine.store.add(row)
            self.schedules[row.id] = row
        await self.engine.bus.emit(
            "schedule.created",
            f"schedule {spec.name!r} created ({spec.cron} {spec.timezone})",
            group_id=spec.group_id,
            data={"schedule_id": row.id},
        )
        self._wake.set()
        return row

    async def update(self, schedule_id: str, patch: schemas.ScheduleUpdate) -> db.ScheduleRow:
        """Changes the fields set in ``patch``; a new cron or time zone reschedules the next run.

        Args:
            schedule_id: The schedule.
            patch: The fields to change.

        Returns:
            The updated schedule.

        Raises:
            ScheduleNotFoundError: No such schedule.
            ValueError: A required field set to null, or an unknown group.
        """
        changes = patch.model_dump(exclude_unset=True)
        for key in ("cron", "timezone", "concurrency_policy", "job"):
            if key in changes and changes[key] is None:
                raise ValueError(f"'{key}' cannot be null")
        async with self._lock:
            row = self.get(schedule_id)
            job = patch.job if "job" in changes else schemas.JobTemplate.model_validate(row.job)
            group_id = changes.get("group_id", row.group_id)
            if "job" in changes or "group_id" in changes:
                self._validate(job, group_id)
            if "job" in changes:
                changes["job"] = job.model_dump(mode="json")
            cron, tz = changes.get("cron", row.cron), changes.get("timezone", row.timezone)
            if row.status == "active" and ("cron" in changes or "timezone" in changes):
                changes["next_run_at"] = cron_lib.next_fire(cron, tz, db.utcnow())
            await self._save(row, **changes)
        self._wake.set()
        return row

    async def delete(self, schedule_id: str, cancel_active: bool = False) -> list[str]:
        """Deletes a schedule and its run history.

        Args:
            schedule_id: The schedule.
            cancel_active: Also cancel the jobs it started that are still running.

        Returns:
            The ids of the jobs cancelled.

        Raises:
            ScheduleNotFoundError: No such schedule.
        """
        async with self._lock:
            row = self.get(schedule_id)
            cancelled = []
            if cancel_active:
                for jid in self.active_jobs(row.id):
                    try:
                        await self.engine.managers[jid].cancel("schedule")
                        cancelled.append(jid)
                    except (job_lib.JobStateError, KeyError):
                        pass
            async with self.engine.store.transaction() as conn:
                await self.engine.store.delete(db.ScheduleRunRow, {"schedule_id": row.id}, conn=conn)
                await self.engine.store.delete(db.ScheduleRow, {"id": row.id}, conn=conn)
            del self.schedules[row.id]
        await self.engine.bus.emit(
            "schedule.deleted",
            f"schedule {row.name!r} deleted",
            group_id=row.group_id,
            data={"schedule_id": row.id, "cancelled_jobs": cancelled},
        )
        return cancelled

    async def suspend(self, schedule_id: str) -> db.ScheduleRow:
        """Stops firing (active runs continue).

        Args:
            schedule_id: The schedule.

        Returns:
            The suspended schedule.

        Raises:
            ScheduleNotFoundError: No such schedule.
            job.JobStateError: Already suspended.
        """
        async with self._lock:
            row = self.get(schedule_id)
            if row.status == "suspended":
                raise job_lib.JobStateError("schedule already suspended")
            await self._save(row, status="suspended", next_run_at=None)
        await self.engine.bus.emit(
            "schedule.suspended",
            f"schedule {row.name!r} suspended",
            group_id=row.group_id,
            data={"schedule_id": row.id},
        )
        return row

    async def resume(self, schedule_id: str) -> db.ScheduleRow:
        """Fire times that passed while suspended are not caught up."""
        async with self._lock:
            row = self.get(schedule_id)
            if row.status == "active":
                raise job_lib.JobStateError("schedule is not suspended")
            await self._save(
                row, status="active", next_run_at=cron_lib.next_fire(row.cron, row.timezone, db.utcnow())
            )
        await self.engine.bus.emit(
            "schedule.resumed",
            f"schedule {row.name!r} resumed",
            group_id=row.group_id,
            data={"schedule_id": row.id},
        )
        self._wake.set()
        return row

    async def trigger(self, schedule_id: str) -> db.ScheduleRunRow:
        """Run now, outside the cron timing (works on suspended schedules, honours the concurrency policy)."""
        async with self._lock:
            row = self.get(schedule_id)
            return await self._fire(row, db.utcnow(), "manual")
