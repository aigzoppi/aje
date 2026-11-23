"""Internal endpoints for dashboards and tools (not part of the stable /v1 API)."""

from __future__ import annotations

import fastapi
from sse_starlette import sse

from aje.application.services import streams as streams_lib
from aje.interfaces.http import deps

router = fastapi.APIRouter(prefix="/internal", tags=["internal"])


@router.get("/events", summary="Server-sent events: every engine event, live job snapshots and task results")
async def events(
    request: fastapi.Request,
    streams: deps.StreamServiceDep,
    job_id: str | None = fastapi.Query(None, description="only this job"),
    group_id: str | None = fastapi.Query(None, description="only jobs of this group"),
    tasks: bool = fastapi.Query(False, description="also stream each task result (event 'task')"),
    snapshot_every: float = fastapi.Query(2.0, ge=0.2, le=60, description="seconds between snapshots"),
    after_id: int | None = fastapi.Query(None, ge=0, description="also replay stored events after this id"),
    last_event_id: str | None = fastapi.Header(None, include_in_schema=False),
):
    """Streams engine events as SSE for dashboards (the Fractal Lab).

    Event types:

    * ``snapshot``: ``{"jobs": [...]}`` live state of the matching jobs (at connect and periodically)
    * ``event``: a persisted event (job.*, rule.*, scale.*, schedule.*, ...)
    * ``task``: ``task.done`` / ``task.failed`` with the worker and the result (``tasks=true`` only)
    * ``reset``: the client fell behind; the stream closes and the reconnect resumes it

    Events carry their id as the SSE id: a reconnecting ``EventSource`` resumes after the last one.

    Browsers' ``EventSource`` cannot send headers: pass the key as ``?api_key=`` when ``AJE_API_KEY`` is set.
    """
    gen = streams.internal_stream(
        request.is_disconnected,
        job_id=job_id,
        group_id=group_id,
        tasks=tasks,
        snapshot_every=snapshot_every,
        after_id=streams_lib.resume_point(last_event_id, after_id),
    )
    return sse.EventSourceResponse(gen)
