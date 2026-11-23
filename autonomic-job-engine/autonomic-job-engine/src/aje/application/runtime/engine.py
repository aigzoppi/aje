"""The Autonomic Job Engine: owns stores, broker, backend and all managers."""

from __future__ import annotations

import logging

from aje import config
from aje.application import schemas
from aje.application.ports import backend as backend_lib
from aje.application.ports import broker as broker_lib
from aje.application.runtime import compiler
from aje.application.runtime import data_parallel
from aje.application.runtime import events
from aje.application.runtime import group
from aje.application.runtime import job as job_lib
from aje.application.runtime import scheduler
from aje.domain import dsl
from aje.infrastructure.persistence import db

log = logging.getLogger("aje.engine")

MANAGERS = {"farm": job_lib.JobManager, "data_parallel": data_parallel.DataParallelManager}


class AutonomicEngine:
    """Owns the store, broker and backend, and every job and group manager.

    Attributes:
        managers: Live job managers by job id.
        groups: Group managers by group id.
        finished: Recently finished job managers (bounded), so their final state stays visible.
        scheduler: The cron scheduler.
        bus: The event bus.
    """

    def __init__(
        self,
        settings: config.Settings,
        store: db.Store,
        broker: broker_lib.Broker,
        backend: backend_lib.Backend,
    ):
        self.settings = settings
        self.store = store
        self.broker = broker
        self.backend = backend
        self.bus = events.EventBus(store)
        self.managers: dict[str, job_lib.JobManager] = {}
        self.groups: dict[str, group.GroupManager] = {}
        self.finished: dict[str, job_lib.JobManager] = {}
        self.scheduler = scheduler.Scheduler(self)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.store.init()
        await self.broker.connect()
        await self.backend.start()
        await self._recover()
        await self.scheduler.start()

    async def stop(self) -> None:
        await self.scheduler.stop()
        for g in self.groups.values():
            await g.stop()
        for m in list(self.managers.values()):
            await m.stop()
        await self.backend.close()
        await self.broker.close()
        await self.store.close()

    async def _recover(self) -> None:
        """Rebuilds the managers of groups and jobs that were live when the engine stopped."""
        for row in await self.store.find(db.GroupRow, {"status": ["running", "suspended"]}):
            rb = (
                dsl.RuleBase.compile(
                    row.contract["source"], dsl.CATALOGS["group"], row.contract.get("globals")
                )
                if row.contract.get("source")
                else None
            )
            g = group.GroupManager(self, row, rb)
            self.groups[g.id] = g
            g.start()
        live = [s.value for s in schemas.JobStatus if s not in schemas.TERMINAL]
        for row in await self.store.find(db.JobRow, {"status": live}):
            try:
                rb = (
                    dsl.RuleBase.compile(
                        row.contract["source"], dsl.CATALOGS[row.pattern], row.contract.get("globals")
                    )
                    if row.contract.get("source")
                    else None
                )
                m = MANAGERS[row.pattern](self, row, rb)
                self.managers[m.id] = m
                await m.start(new=False)
            except Exception:  # pylint: disable=broad-exception-caught  # one broken job must not stop the others from recovering
                log.exception("could not recover job %s", row.id)
        if self.managers:
            log.info("recovered %d job(s), %d group(s)", len(self.managers), len(self.groups))

    # ------------------------------------------------------------------ budget
    def allowance(self, job: job_lib.JobManager, requested: int) -> int:
        g = self.groups.get(job.group_id) if job.group_id else None
        return g.allowance(job, requested) if g else requested

    def budget_free(self, job: job_lib.JobManager) -> int | None:
        g = self.groups.get(job.group_id) if job.group_id else None
        return g.free() if g else None

    def on_job_finished(self, m: job_lib.JobManager) -> None:
        self.managers.pop(m.id, None)
        self.finished[m.id] = m
        if len(self.finished) > 500:
            self.finished.pop(next(iter(self.finished)))

    # ------------------------------------------------------------------ submit
    async def create_job(self, spec: schemas.JobCreate, group_id: str | None = None) -> job_lib.JobManager:
        """Submits a job: persists it, starts its manager and feeds its initial input.

        Args:
            spec: The job specification.
            group_id: The group to add the job to; its budget limits the initial workers.

        Returns:
            The job's manager (already finished if its workers could not start).

        Raises:
            dsl.ContractError: The contract is invalid.
            ValueError: The group does not accept jobs.
        """
        rulebase, info = compiler.compile_contract(spec.pattern, spec.contract)
        if group_id:
            g = self.groups.get(group_id)
            if g is None or g.status not in ("running", "finished"):
                raise ValueError(f"group {group_id} is not accepting jobs")
            if g.status == "finished":
                await g.set_status("running")
        if spec.output_queue is None:  # a fresh stream, independent of the job id
            spec = spec.model_copy(update={"output_queue": job_lib.new_id(32)})
        row = db.JobRow(
            id=job_lib.new_id(),
            group_id=group_id,
            name=spec.name,
            pattern=spec.pattern,
            status=schemas.JobStatus.PENDING.value,
            management=spec.management,
            workers=spec.workers,
            partitions=spec.partitions if spec.pattern == "data_parallel" else None,
            closed=False,
            spec=spec.model_dump(mode="json"),
            contract=info,
        )
        m = MANAGERS[spec.pattern](self, row, rulebase)
        if group_id:  # fit the initial size into the budget
            self.managers[m.id] = m
            m.workers = 0  # not holding any budget yet
            row.workers = m.workers = self.allowance(m, spec.workers)
            if m.workers < spec.workers:
                m.raise_escalation("budget", spec.workers - m.workers, "submit")
        await self.store.add(row)
        self.managers[m.id] = m
        await self.bus.emit(
            "job.submitted", f"{spec.pattern} job {spec.name!r} submitted", job_id=m.id, group_id=group_id
        )
        await m.start(new=True)
        if m.status in schemas.TERMINAL:
            return m
        if spec.pattern == "farm" and spec.tasks:
            await m.submit_tasks(spec.tasks)
        if spec.pattern == "data_parallel" and spec.items:
            await m.submit_batch(spec.items)
        if spec.close_when_done:
            await m.close()
        return m

    async def create_group(self, spec: schemas.GroupCreate) -> group.GroupManager:
        """Creates a group and submits its member jobs.

        Every contract (the group's and the members') is validated before anything is created.

        Args:
            spec: The group specification with its member jobs.

        Returns:
            The group's manager.

        Raises:
            dsl.ContractError: A contract is invalid.
        """
        rulebase, info = compiler.compile_contract("group", spec.contract)
        # validate every member contract before creating anything
        for j in spec.jobs:
            compiler.compile_contract(j.pattern, j.contract)
        row = db.GroupRow(
            id=job_lib.new_id(),
            name=spec.name,
            status="running",
            management=spec.management,
            max_workers=spec.max_workers,
            contract=info,
        )
        await self.store.add(row)
        g = group.GroupManager(self, row, rulebase)
        self.groups[g.id] = g
        await self.bus.emit(
            "group.created", f"group {spec.name!r} created (budget {spec.max_workers})", group_id=g.id
        )
        for j in spec.jobs:
            await self.create_job(j, group_id=g.id)
        g.start()
        return g

    # ------------------------------------------------------------------ lookup
    def job(self, job_id: str) -> job_lib.JobManager | None:
        return self.managers.get(job_id) or self.finished.get(job_id)
