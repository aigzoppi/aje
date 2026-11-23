"""HTTP adapter for job use cases: parse the request, call JobService, return its DTO."""

from __future__ import annotations

from typing import Any

import fastapi
from fastapi import status
from sse_starlette import sse

from aje.application import schemas
from aje.application.services import streams as streams_lib
from aje.interfaces.http import deps

router = fastapi.APIRouter(prefix="/v1/jobs", tags=["jobs"])


# ------------------------------------------------------------------ submit / query
@router.post(
    "",
    response_model=schemas.JobOut,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a job (farm or data_parallel)",
)
async def submit_job(spec: schemas.JobCreate, jobs: deps.JobServiceDep):
    return await jobs.submit(spec)


@router.get("", response_model=list[schemas.JobOut], summary="List jobs")
async def list_jobs(
    jobs: deps.JobServiceDep,
    status_: str | None = fastapi.Query(None, alias="status"),
    pattern: str | None = None,
    group_id: str | None = None,
    limit: int = fastapi.Query(100, le=1000),
    offset: int = 0,
):
    return await jobs.list(status=status_, pattern=pattern, group_id=group_id, limit=limit, offset=offset)


@router.get("/{job_id}", response_model=schemas.JobOut, summary="Job status, counters and latest metrics")
async def get_job(job_id: str, jobs: deps.JobServiceDep):
    return await jobs.get(job_id)


@router.delete("/{job_id}", response_model=schemas.JobOut, summary="Cancel a job")
async def cancel_job(job_id: str, jobs: deps.JobServiceDep):
    return await jobs.cancel(job_id)


# ------------------------------------------------------------------ input
@router.post("/{job_id}/tasks", status_code=status.HTTP_202_ACCEPTED, summary="Farm: add tasks to the stream")
async def add_tasks(job_id: str, body: schemas.TasksIn, jobs: deps.JobServiceDep):
    return await jobs.add_tasks(job_id, body.tasks, body.close)


@router.get("/{job_id}/tasks", response_model=list[schemas.TaskOut], summary="List tasks")
async def list_tasks(
    job_id: str,
    jobs: deps.JobServiceDep,
    status_: str | None = fastapi.Query(None, alias="status"),
    include_payload: bool = False,
    limit: int = fastapi.Query(100, le=5000),
    offset: int = 0,
):
    return await jobs.list_tasks(
        job_id, status=status_, include_payload=include_payload, limit=limit, offset=offset
    )


@router.get("/{job_id}/tasks/{task_id}", response_model=schemas.TaskOut, summary="One task with its result")
async def get_task(job_id: str, task_id: str, jobs: deps.JobServiceDep):
    return await jobs.get_task(job_id, task_id)


@router.post(
    "/{job_id}/batches",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Data-parallel: scatter a batch of items",
)
async def add_batch(job_id: str, body: schemas.BatchIn, jobs: deps.JobServiceDep):
    return await jobs.add_batch(job_id, body.items, body.partitions)


@router.get("/{job_id}/batches", response_model=list[schemas.BatchOut], summary="List batches")
async def list_batches(
    job_id: str,
    jobs: deps.JobServiceDep,
    include_result: bool = False,
    limit: int = fastapi.Query(100, le=1000),
    offset: int = 0,
):
    return await jobs.list_batches(job_id, include_result=include_result, limit=limit, offset=offset)


@router.get(
    "/{job_id}/batches/{batch_id}",
    response_model=schemas.BatchOut,
    summary="One batch with its gathered result",
)
async def get_batch(job_id: str, batch_id: str, jobs: deps.JobServiceDep):
    return await jobs.get_batch(job_id, batch_id)


@router.post(
    "/{job_id}/close",
    response_model=schemas.JobOut,
    summary="No more input: the job succeeds once outstanding work is done",
)
async def close_job(job_id: str, jobs: deps.JobServiceDep):
    return await jobs.close(job_id)


# ------------------------------------------------------------------ control interface
@router.post("/{job_id}/scale", response_model=schemas.JobOut, summary="Set the number of workers")
async def scale(job_id: str, body: schemas.ScaleIn, jobs: deps.JobServiceDep):
    return await jobs.scale(job_id, body.workers)


@router.post(
    "/{job_id}/partitions",
    response_model=schemas.JobOut,
    summary="Data-parallel: set partitions for next batches",
)
async def partitions(job_id: str, body: schemas.PartitionsIn, jobs: deps.JobServiceDep):
    return await jobs.set_partitions(job_id, body.partitions)


@router.post(
    "/{job_id}/suspend", response_model=schemas.JobOut, summary="Suspend all workers (queued work is kept)"
)
async def suspend(job_id: str, jobs: deps.JobServiceDep):
    return await jobs.suspend(job_id)


@router.post("/{job_id}/resume", response_model=schemas.JobOut, summary="Resume a suspended job")
async def resume(job_id: str, jobs: deps.JobServiceDep):
    return await jobs.resume(job_id)


@router.put(
    "/{job_id}/management",
    response_model=schemas.JobOut,
    summary="Switch between active (contract-driven) and passive (API-driven) management",
)
async def management(job_id: str, body: schemas.ManagementIn, jobs: deps.JobServiceDep):
    return await jobs.set_management(job_id, body.management)


@router.get("/{job_id}/contract", summary="The job's contract, including its source")
async def get_contract(job_id: str, jobs: deps.JobServiceDep):
    return await jobs.get_contract(job_id)


@router.put("/{job_id}/contract", summary="Replace the contract of a running job (hot swap)")
async def put_contract(job_id: str, body: schemas.ContractSpec, jobs: deps.JobServiceDep):
    return await jobs.put_contract(job_id, body)


# ------------------------------------------------------------------ monitoring
@router.get(
    "/{job_id}/metrics",
    response_model=schemas.JobMetricsOut,
    summary="Current metrics (the Metrics fact) and recent history",
)
async def metrics(job_id: str, jobs: deps.JobServiceDep):
    return jobs.metrics(job_id)


@router.get("/{job_id}/facts", summary="The working memory the contract sees at the last tick")
async def facts(job_id: str, jobs: deps.JobServiceDep):
    return jobs.facts(job_id)


@router.get(
    "/{job_id}/events", response_model=list[schemas.EventOut], summary="Lifecycle and management events"
)
async def events(
    job_id: str,
    jobs: deps.JobServiceDep,
    kind: str | None = None,
    after_id: int = 0,
    limit: int = fastapi.Query(200, le=5000),
):
    return await jobs.events_of(job_id, kind=kind, after_id=after_id, limit=limit)


@router.get(
    "/{job_id}/stream",
    summary="Server-sent events: the job's history and live events, metrics and (optionally) task results",
    responses={204: {"description": "the job is over and there is nothing after the resume point"}},
)
async def stream(
    job_id: str,
    request: fastapi.Request,
    streams: deps.StreamServiceDep,
    metrics_every: float = fastapi.Query(5.0, ge=0.5),
    tasks: bool = fastapi.Query(False, description="also stream each task result (event 'task')"),
    after_id: int | None = fastapi.Query(
        None, ge=0, description="replay stored events after this id (default: the whole history)"
    ),
    last_event_id: str | None = fastapi.Header(None, include_in_schema=False),
):
    """Streams the job: ``event`` (with an SSE id), ``metrics``, ``task`` (if asked), ``end``, ``reset``.

    Stored events after the resume point are replayed first: ``Last-Event-ID`` (sent by a
    reconnecting ``EventSource``) or ``after_id``; by default the whole history. ``end`` carries
    the final status; a request after the end gets 204, so the browser stops reconnecting.
    """
    gen = await streams.job_stream(
        job_id,
        request.is_disconnected,
        metrics_every=metrics_every,
        after_id=streams_lib.resume_point(last_event_id, after_id),
        tasks=tasks,
    )
    if gen is None:
        return fastapi.Response(status_code=status.HTTP_204_NO_CONTENT)
    return sse.EventSourceResponse(gen)


@router.get("/{job_id}/workers", summary="Worker pods / processes")
async def workers(job_id: str, jobs: deps.JobServiceDep) -> list[dict[str, Any]]:
    return await jobs.workers(job_id)


@router.get("/{job_id}/logs", summary="Worker logs")
async def logs(
    job_id: str,
    jobs: deps.JobServiceDep,
    worker: str | None = None,
    tail: int = fastapi.Query(100, ge=1, le=5000),
) -> dict[str, str]:
    return await jobs.logs(job_id, worker, tail)
