"""Group use cases: jobs that share a worker budget under a second-level contract."""

from __future__ import annotations

from typing import Any, Literal

from aje.application import schemas
from aje.application.ports import repositories
from aje.application.runtime import compiler
from aje.application.runtime import engine as engine_lib
from aje.application.runtime import group
from aje.application.services import jobs as jobs_lib
from aje.application.services import presenters
from aje.domain import errors

MemberOp = Literal["suspend", "resume", "cancel"]


class GroupService:
    """Use cases on groups: create, read, budget, management, contract, bulk operations."""

    def __init__(
        self,
        engine: engine_lib.AutonomicEngine,
        groups: repositories.GroupRepository,
        jobs: repositories.JobRepository,
        events: repositories.EventRepository,
        job_service: jobs_lib.JobService,
    ):
        self.engine = engine
        self.groups = groups
        self.jobs = jobs
        self.events = events
        self.job_service = job_service

    def live(self, group_id: str) -> group.GroupManager:
        g = self.engine.groups.get(group_id)
        if g is None:
            raise errors.NotFoundError(f"no group {group_id}")
        return g

    async def view(self, group_id: str) -> schemas.GroupOut:
        row = await self.groups.get(group_id)
        if row is None:
            raise errors.NotFoundError(f"no group {group_id}")
        members = [await self.job_service.present(j) for j in await self.jobs.list_by_group(group_id)]
        return presenters.group_out(row, members, self.engine)

    async def create(self, spec: schemas.GroupCreate) -> schemas.GroupOut:
        g = await self.engine.create_group(spec)
        return await self.view(g.id)

    async def list(self, *, limit: int = 50) -> list[schemas.GroupOut]:
        return [await self.view(r.id) for r in await self.groups.list(limit=limit)]

    async def add_job(self, group_id: str, spec: schemas.JobCreate) -> schemas.JobOut:
        g = self.live(group_id)
        return await self.job_service.submit(spec, group_id=g.id)

    async def member_op(self, group_id: str, op: MemberOp) -> dict[str, Any]:
        g = self.live(group_id)
        return {"group_id": g.id, "status": g.status, "results": await g.for_members(op, "api")}

    async def set_budget(self, group_id: str, max_workers: int | None) -> schemas.GroupOut:
        await self.live(group_id).set_budget(max_workers, "api")
        return await self.view(group_id)

    async def set_management(self, group_id: str, management: str) -> schemas.GroupOut:
        await self.live(group_id).set_management(management)
        return await self.view(group_id)

    def get_contract(self, group_id: str) -> dict[str, Any]:
        return presenters.contract_view(self.live(group_id).contract_info, include_source=True)

    async def put_contract(self, group_id: str, spec: schemas.ContractSpec) -> dict[str, Any]:
        g = self.live(group_id)
        rulebase, info = compiler.compile_contract("group", spec)
        await g.set_contract(rulebase, info)
        return presenters.contract_view(info, include_source=True)

    def facts(self, group_id: str) -> dict[str, Any]:
        return self.live(group_id).facts()

    async def events_of(
        self, group_id: str, *, after_id: int = 0, limit: int = 200
    ) -> list[schemas.EventOut]:
        return [
            presenters.event_out(r)
            for r in await self.events.list(group_id=group_id, after_id=after_id, limit=limit)
        ]
