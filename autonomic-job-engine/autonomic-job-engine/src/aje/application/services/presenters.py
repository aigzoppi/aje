"""Presenters: build output DTOs from stored rows and, when the job is live, its manager."""

from __future__ import annotations

from aje.application import schemas
from aje.application.runtime import engine as engine_lib
from aje.infrastructure.persistence import db


def contract_view(info: dict, include_source: bool = False) -> dict:
    out = {k: info.get(k) for k in ("builtin", "globals", "rules", "parameters")}
    if include_source:
        out["source"] = info.get("source")
    return out


def job_out(row: db.JobRow, engine: engine_lib.AutonomicEngine, counts: dict | None = None) -> schemas.JobOut:
    m = engine.managers.get(row.id)
    spec = row.spec
    return schemas.JobOut(
        id=row.id,
        name=row.name,
        pattern=row.pattern,
        status=m.status.value if m else row.status,
        group_id=row.group_id,
        management=m.management if m else row.management,
        workers=m.workers if m else row.workers,
        min_workers=spec["min_workers"],
        max_workers=spec["max_workers"],
        partitions=getattr(m, "partitions", None) if m else row.partitions,
        closed=m.closed if m else row.closed,
        message=m.message if m and m.message else row.message,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
        contract=contract_view(m.contract_info if m else row.contract),
        counts=m.counts() if m else (row.counts or counts or {}),
        metrics=m.last_metrics if m else None,
        jobcontext=spec.get("jobcontext") or {},
        output_queue=spec.get("output_queue"),
    )


def group_out(
    row: db.GroupRow, jobs: list[schemas.JobOut], engine: engine_lib.AutonomicEngine
) -> schemas.GroupOut:
    g = engine.groups.get(row.id)
    return schemas.GroupOut(
        id=row.id,
        name=row.name,
        status=g.status if g else row.status,
        management=g.management if g else row.management,
        max_workers=g.max_workers if g else row.max_workers,
        used_workers=g.used_workers() if g else 0,
        created_at=row.created_at,
        jobs=jobs,
        contract=contract_view(g.contract_info if g else row.contract),
    )


def task_out(r: db.TaskRow, include_payload: bool = True) -> schemas.TaskOut:
    return schemas.TaskOut(
        id=r.id,
        job_id=r.job_id,
        status=r.status,
        attempts=r.attempts,
        batch_id=r.batch_id,
        partition=r.partition,
        payload=r.payload if include_payload else None,
        result=r.result,
        error=r.error,
        worker=r.worker,
        submitted_at=r.submitted_at,
        finished_at=r.finished_at,
    )


def batch_out(r: db.BatchRow, include_result: bool = True) -> schemas.BatchOut:
    return schemas.BatchOut(
        id=r.id,
        job_id=r.job_id,
        status=r.status,
        size=r.size,
        partitions=r.partitions,
        completed_partitions=r.completed_partitions,
        result=r.result if include_result else None,
        error=r.error,
        created_at=r.created_at,
        finished_at=r.finished_at,
        duration=r.duration,
    )


def event_out(r: db.EventRow) -> schemas.EventOut:
    return schemas.EventOut(
        id=r.id, ts=r.ts, job_id=r.job_id, group_id=r.group_id, kind=r.kind, message=r.message, data=r.data
    )


def schedule_out(row: db.ScheduleRow, active_jobs: list[str]) -> schemas.ScheduleOut:
    return schemas.ScheduleOut(
        id=row.id,
        name=row.name,
        cron=row.cron,
        timezone=row.timezone,
        status=row.status,
        concurrency_policy=row.concurrency_policy,
        starting_deadline_seconds=row.starting_deadline_seconds,
        group_id=row.group_id,
        job=row.job,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        last_job_id=row.last_job_id,
        active_jobs=active_jobs,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def schedule_run_out(r: db.ScheduleRunRow, job_status: str | None) -> schemas.ScheduleRunOut:
    return schemas.ScheduleRunOut(
        id=r.id,
        schedule_id=r.schedule_id,
        scheduled_at=r.scheduled_at,
        created_at=r.created_at,
        trigger=r.trigger,
        status=r.status,
        job_id=r.job_id,
        job_status=job_status,
        message=r.message,
    )
