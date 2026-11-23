"""HTTP adapter for group use cases."""

from __future__ import annotations

from typing import Literal

import fastapi
from fastapi import status
from sse_starlette import sse

from aje.application import schemas
from aje.application.services import streams as streams_lib
from aje.interfaces.http import deps

router = fastapi.APIRouter(prefix="/v1/groups", tags=["groups"])


@router.post(
    "",
    response_model=schemas.GroupOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a group of jobs sharing a worker budget",
)
async def create_group(spec: schemas.GroupCreate, groups: deps.GroupServiceDep):
    return await groups.create(spec)


@router.get("", response_model=list[schemas.GroupOut], summary="List groups")
async def list_groups(groups: deps.GroupServiceDep, limit: int = fastapi.Query(50, le=500)):
    return await groups.list(limit=limit)


@router.get("/{group_id}", response_model=schemas.GroupOut, summary="Group status with member jobs")
async def get_group(group_id: str, groups: deps.GroupServiceDep):
    return await groups.view(group_id)


@router.post(
    "/{group_id}/jobs",
    response_model=schemas.JobOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a job to the group",
)
async def add_job(group_id: str, spec: schemas.JobCreate, groups: deps.GroupServiceDep):
    return await groups.add_job(group_id, spec)


@router.post("/{group_id}/{op}", summary="suspend | resume | cancel every member job")
async def member_op(group_id: str, op: Literal["suspend", "resume", "cancel"], groups: deps.GroupServiceDep):
    return await groups.member_op(group_id, op)


@router.put("/{group_id}/budget", response_model=schemas.GroupOut, summary="Change the worker budget")
async def budget(group_id: str, body: schemas.BudgetIn, groups: deps.GroupServiceDep):
    return await groups.set_budget(group_id, body.max_workers)


@router.put(
    "/{group_id}/management", response_model=schemas.GroupOut, summary="active | passive group management"
)
async def management(group_id: str, body: schemas.ManagementIn, groups: deps.GroupServiceDep):
    return await groups.set_management(group_id, body.management)


@router.get("/{group_id}/contract", summary="The group contract")
async def get_contract(group_id: str, groups: deps.GroupServiceDep):
    return groups.get_contract(group_id)


@router.put("/{group_id}/contract", summary="Replace the group contract")
async def put_contract(group_id: str, body: schemas.ContractSpec, groups: deps.GroupServiceDep):
    return await groups.put_contract(group_id, body)


@router.get("/{group_id}/facts", summary="Working memory of the group contract")
async def facts(group_id: str, groups: deps.GroupServiceDep):
    return groups.facts(group_id)


@router.get(
    "/{group_id}/events", response_model=list[schemas.EventOut], summary="Events of the group and its members"
)
async def events(
    group_id: str, groups: deps.GroupServiceDep, after_id: int = 0, limit: int = fastapi.Query(200, le=5000)
):
    return await groups.events_of(group_id, after_id=after_id, limit=limit)


@router.get(
    "/{group_id}/stream",
    summary="Server-sent events of the group and its members: history, live events, facts",
    responses={204: {"description": "the group is over and there is nothing after the resume point"}},
)
async def stream(
    group_id: str,
    request: fastapi.Request,
    streams: deps.StreamServiceDep,
    metrics_every: float = fastapi.Query(5.0, ge=0.5),
    after_id: int | None = fastapi.Query(
        None, ge=0, description="replay stored events after this id (default: the whole history)"
    ),
    last_event_id: str | None = fastapi.Header(None, include_in_schema=False),
):
    """Streams the group: ``event`` (with an SSE id), ``facts``, ``end``, ``reset`` (see the job stream)."""
    gen = await streams.group_stream(
        group_id,
        request.is_disconnected,
        metrics_every=metrics_every,
        after_id=streams_lib.resume_point(last_event_id, after_id),
    )
    if gen is None:
        return fastapi.Response(status_code=status.HTTP_204_NO_CONTENT)
    return sse.EventSourceResponse(gen)
