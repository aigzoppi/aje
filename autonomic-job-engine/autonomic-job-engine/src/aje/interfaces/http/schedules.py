"""HTTP adapter for cron schedules."""

from __future__ import annotations

import fastapi
from fastapi import status

from aje.application import schemas
from aje.interfaces.http import deps

router = fastapi.APIRouter(prefix="/v1/schedules", tags=["schedules"])


@router.post(
    "",
    response_model=schemas.ScheduleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a cron schedule that launches a job from a template",
)
async def create_schedule(spec: schemas.ScheduleCreate, schedules: deps.ScheduleServiceDep):
    return await schedules.create(spec)


@router.get("", response_model=list[schemas.ScheduleOut], summary="List schedules")
async def list_schedules(
    schedules: deps.ScheduleServiceDep,
    status_: str | None = fastapi.Query(None, alias="status", pattern="^(active|suspended)$"),
    group_id: str | None = None,
):
    return schedules.list(status=status_, group_id=group_id)


@router.get(
    "/{schedule_id}", response_model=schemas.ScheduleOut, summary="One schedule with its next fire time"
)
async def get_schedule(schedule_id: str, schedules: deps.ScheduleServiceDep):
    return schedules.get(schedule_id)


@router.patch(
    "/{schedule_id}",
    response_model=schemas.ScheduleOut,
    summary="Change cron, timezone, policy, group or job template",
)
async def update_schedule(schedule_id: str, body: schemas.ScheduleUpdate, schedules: deps.ScheduleServiceDep):
    return await schedules.update(schedule_id, body)


@router.delete("/{schedule_id}", summary="Delete a schedule (and optionally cancel its active runs)")
async def delete_schedule(
    schedule_id: str,
    schedules: deps.ScheduleServiceDep,
    cancel_active: bool = fastapi.Query(False, description="also cancel jobs still running"),
):
    return await schedules.delete(schedule_id, cancel_active)


@router.post(
    "/{schedule_id}/suspend", response_model=schemas.ScheduleOut, summary="Stop firing (active runs continue)"
)
async def suspend_schedule(schedule_id: str, schedules: deps.ScheduleServiceDep):
    return await schedules.suspend(schedule_id)


@router.post(
    "/{schedule_id}/resume",
    response_model=schemas.ScheduleOut,
    summary="Fire again from the next cron time (missed times are not caught up)",
)
async def resume_schedule(schedule_id: str, schedules: deps.ScheduleServiceDep):
    return await schedules.resume(schedule_id)


@router.post(
    "/{schedule_id}/trigger",
    response_model=schemas.ScheduleRunOut,
    status_code=status.HTTP_201_CREATED,
    summary="Launch a run now (honours the concurrency policy)",
)
async def trigger_schedule(schedule_id: str, schedules: deps.ScheduleServiceDep):
    return await schedules.trigger(schedule_id)


@router.get(
    "/{schedule_id}/runs",
    response_model=list[schemas.ScheduleRunOut],
    summary="Run history: started, skipped and failed runs with their job status",
)
async def list_runs(
    schedule_id: str,
    schedules: deps.ScheduleServiceDep,
    status_: str | None = fastapi.Query(None, alias="status", pattern="^(started|skipped|failed)$"),
    limit: int = fastapi.Query(100, le=1000),
    offset: int = 0,
):
    """Lists the runs of a schedule, newest first, with the status of the job each started."""
    return await schedules.runs(schedule_id, status=status_, limit=limit, offset=offset)
