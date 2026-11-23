"""Live streams (Server-Sent Events) built on the event bus and the event log.

Each generator yields ``{"event": <name>, "data": <json text>, "id"?: <event id>}`` messages
and is transport agnostic: the API layer wraps it in an ``EventSourceResponse``.

Every stream follows the same recipe, so a client never misses or duplicates an event:

1. subscribe to the bus (live events are queued from now on);
2. replay the stored events after the client's resume point (``Last-Event-ID`` sent by a
   reconnecting ``EventSource``, or ``?after_id=``) from the event log;
3. go live, skipping queued events already replayed (by id).

Events carry their id as the SSE ``id``, so a reconnecting browser resumes by itself. Job and
group streams end with ``end`` (and the final status) once the job or group is over; asking
again then gets no stream at all (HTTP 204), which stops ``EventSource`` from reconnecting.
A subscriber that falls too far behind gets ``reset`` and the stream closes; its reconnect
replays what it missed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import json
from typing import Any

from aje.application import schemas
from aje.application.ports import repositories
from aje.application.runtime import engine as engine_lib
from aje.application.runtime import events
from aje.application.runtime import job
from aje.domain import errors

Disconnected = Callable[[], Awaitable[bool]]
Message = dict[str, str]

REPLAY_PAGE = 500
JOB_TERMINAL_KINDS = frozenset(f"job.{s.value}" for s in schemas.TERMINAL)
GROUP_TERMINAL = frozenset({"finished", "cancelled"})
GROUP_TERMINAL_KINDS = frozenset(f"group.{s}" for s in GROUP_TERMINAL)


def resume_point(last_event_id: str | None, after_id: int | None) -> int | None:
    """Where a stream resumes: the ``Last-Event-ID`` of a reconnecting client wins over ``after_id``.

    Args:
        last_event_id: The ``Last-Event-ID`` header, if any (ignored unless a non-negative integer).
        after_id: The ``after_id`` query parameter, if any.

    Returns:
        The id after which stored events are replayed, or None for no replay preference.
    """
    if last_event_id:
        try:
            return max(0, int(last_event_id))
        except ValueError:
            pass
    return after_id


def message(event: str, data: Any, event_id: int | None = None) -> Message:
    """One SSE message; ``event_id`` becomes the SSE ``id`` (stored events only)."""
    msg = {"event": event, "data": json.dumps(data, default=str)}
    if event_id is not None:
        msg["id"] = str(event_id)
    return msg


def typed_metrics(m: job.JobManager) -> dict[str, Any] | None:
    """The job's last metrics validated as ``schemas.JobMetrics`` (None before the first tick)."""
    if m.last_metrics is None:
        return None
    return schemas.JobMetrics.model_validate(m.last_metrics).model_dump()


def job_snapshot(m: job.JobManager) -> dict[str, Any]:
    """What a dashboard needs to draw one job, read from memory (no database access)."""
    return {
        "id": m.id,
        "name": m.name,
        "pattern": m.pattern,
        "status": m.status.value,
        "group_id": m.group_id,
        "management": m.management,
        "workers": m.workers,
        "closed": m.closed,
        "counts": m.counts(),
        "metrics": typed_metrics(m),
    }


def _stored(row) -> dict[str, Any]:
    """A stored event in the shape the bus pushes live ones."""
    return {
        "id": row.id,
        "ts": row.ts.isoformat(),
        "job_id": row.job_id,
        "group_id": row.group_id,
        "kind": row.kind,
        "message": row.message,
        "data": row.data,
    }


class Cursor:
    """The id of the last event sent; filters out live events that were already replayed."""

    def __init__(self, after: int):
        self.last = after

    def accept(self, event: dict[str, Any]) -> bool:
        if event["id"] is None:  # live event that could not be stored: deliver it anyway
            return True
        if event["id"] <= self.last:
            return False
        self.last = event["id"]
        return True


def _reset(cursor: Cursor) -> Message:
    return message(
        "reset", {"reason": "the client fell behind; reconnect to resume", "after_id": cursor.last}
    )


class StreamService:
    """Server-sent event streams: per job, per group, and the internal dashboard feed."""

    def __init__(
        self,
        engine: engine_lib.AutonomicEngine,
        jobs: repositories.JobRepository,
        groups: repositories.GroupRepository,
        events_repo: repositories.EventRepository,
    ):
        self.engine = engine
        self.jobs = jobs
        self.groups = groups
        self.events = events_repo

    async def _replay(
        self, cursor: Cursor, *, job_id: str | None = None, group_id: str | None = None
    ) -> AsyncIterator[Message]:
        """Yields the stored events after ``cursor``, oldest first, advancing it."""
        while True:
            rows = await self.events.list(
                job_id=job_id, group_id=group_id, after_id=cursor.last, limit=REPLAY_PAGE
            )
            for row in rows:
                event = _stored(row)
                if cursor.accept(event):
                    yield message("event", event, event["id"])
            if len(rows) < REPLAY_PAGE:
                return

    async def _has_events_after(
        self, after: int, *, job_id: str | None = None, group_id: str | None = None
    ) -> bool:
        return bool(await self.events.list(job_id=job_id, group_id=group_id, after_id=after, limit=1))

    # ------------------------------------------------------------------ jobs
    async def job_stream(
        self,
        job_id: str,
        disconnected: Disconnected,
        *,
        metrics_every: float = 5.0,
        after_id: int | None = None,
        tasks: bool = False,
    ) -> AsyncIterator[Message] | None:
        """Opens the stream of one job.

        Args:
            job_id: The job.
            disconnected: Tells whether the client went away.
            metrics_every: Seconds between ``metrics`` messages.
            after_id: Replay stored events after this id; None replays the whole history.
            tasks: Also stream a ``task`` message per task result.

        Returns:
            The stream, or None when the job is over and has nothing after ``after_id``
            (the API answers 204, which stops a browser's reconnection).

        Raises:
            errors.NotFoundError: No such job.
        """
        if await self.jobs.get(job_id) is None:
            raise errors.NotFoundError(f"no job {job_id}")
        after = after_id or 0
        if job_id not in self.engine.managers and not await self._has_events_after(after, job_id=job_id):
            return None
        return self._job_stream(job_id, disconnected, metrics_every, after, tasks)

    async def _job_stream(
        self, job_id: str, disconnected: Disconnected, metrics_every: float, after: int, tasks: bool
    ) -> AsyncIterator[Message]:
        """Replay, then live events and metrics until the job ends."""
        sub = self.engine.bus.subscribe(job_id=job_id, notifications=tasks)
        cursor = Cursor(after)
        try:
            async for msg in self._replay(cursor, job_id=job_id):
                yield msg
            if job_id not in self.engine.managers:  # already over: the replay was the whole story
                yield await self._job_end(job_id)
                return
            loop = asyncio.get_running_loop()
            next_metrics = loop.time()
            while not await disconnected():
                if sub.overflowed:
                    yield _reset(cursor)
                    return
                try:
                    channel, event = await asyncio.wait_for(
                        sub.queue.get(), max(0.0, next_metrics - loop.time())
                    )
                except TimeoutError:
                    m = self.engine.managers.get(job_id)
                    if m is None:  # ended without its terminal event reaching us
                        yield await self._job_end(job_id)
                        return
                    yield message(
                        "metrics",
                        {"workers": m.workers, "status": m.status.value, **(typed_metrics(m) or {})},
                    )
                    next_metrics = loop.time() + metrics_every
                    continue
                if channel == events.TASK:
                    yield message("task", event)
                    continue
                if not cursor.accept(event):
                    continue
                yield message("event", event, event["id"])
                if event["kind"] in JOB_TERMINAL_KINDS:
                    yield await self._job_end(job_id)
                    return
        finally:
            self.engine.bus.unsubscribe(sub)

    async def _job_end(self, job_id: str) -> Message:
        m = self.engine.job(job_id)
        if m is not None:
            status = m.status.value
        else:
            row = await self.jobs.get(job_id)
            status = row.status if row else None
        return message("end", {"job_id": job_id, "status": status})

    # ------------------------------------------------------------------ groups
    def _group_status(self, group_id: str, stored: str) -> str:
        g = self.engine.groups.get(group_id)
        return g.status if g is not None else stored

    async def group_stream(
        self,
        group_id: str,
        disconnected: Disconnected,
        *,
        metrics_every: float = 5.0,
        after_id: int | None = None,
    ) -> AsyncIterator[Message] | None:
        """Opens the stream of a group: its events and its member jobs' events.

        Args:
            group_id: The group.
            disconnected: Tells whether the client went away.
            metrics_every: Seconds between ``facts`` messages.
            after_id: Replay stored events after this id; None replays the whole history.

        Returns:
            The stream, or None when the group is finished or cancelled and has nothing
            after ``after_id`` (the API answers 204).

        Raises:
            errors.NotFoundError: No such group.
        """
        row = await self.groups.get(group_id)
        if row is None:
            raise errors.NotFoundError(f"no group {group_id}")
        after = after_id or 0
        over = self._group_status(group_id, row.status) in GROUP_TERMINAL
        if over and not await self._has_events_after(after, group_id=group_id):
            return None
        return self._group_stream(group_id, row.status, disconnected, metrics_every, after)

    async def _group_stream(
        self, group_id: str, stored_status: str, disconnected: Disconnected, metrics_every: float, after: int
    ) -> AsyncIterator[Message]:
        """Replay, current facts, then live events and facts until the group is over."""
        sub = self.engine.bus.subscribe(group_id=group_id)
        cursor = Cursor(after)
        try:
            async for msg in self._replay(cursor, group_id=group_id):
                yield msg
            status = self._group_status(group_id, stored_status)
            if status in GROUP_TERMINAL:
                yield message("end", {"group_id": group_id, "status": status})
                return
            g = self.engine.groups.get(group_id)
            if g is not None:
                yield message("facts", g.facts())
            while not await disconnected():
                if sub.overflowed:
                    yield _reset(cursor)
                    return
                try:
                    _, event = await asyncio.wait_for(sub.queue.get(), metrics_every)
                except TimeoutError:
                    g = self.engine.groups.get(group_id)
                    if g is not None:
                        yield message("facts", g.facts())
                    continue
                if not cursor.accept(event):
                    continue
                yield message("event", event, event["id"])
                if event["kind"] in GROUP_TERMINAL_KINDS and event["job_id"] is None:
                    yield message("end", {"group_id": group_id, "status": event["kind"].split(".", 1)[1]})
                    return
        finally:
            self.engine.bus.unsubscribe(sub)

    # ------------------------------------------------------------------ internal firehose
    def snapshot(self, job_id: str | None = None, group_id: str | None = None) -> list[dict[str, Any]]:
        if job_id:
            m = self.engine.job(job_id)  # also finished jobs, so a dashboard sees the final state
            return [job_snapshot(m)] if m else []
        return [
            job_snapshot(m) for m in self.engine.managers.values() if not group_id or m.group_id == group_id
        ]

    async def internal_stream(
        self,
        disconnected: Disconnected,
        *,
        job_id: str | None = None,
        group_id: str | None = None,
        tasks: bool = False,
        snapshot_every: float = 2.0,
        after_id: int | None = None,
    ) -> AsyncIterator[Message]:
        """Streams every event of the engine (optionally of one job or group).

        Messages:

        * ``snapshot``: live state of the matching jobs, at connect time and every ``snapshot_every`` s
        * ``event``: persisted lifecycle / management events (job.*, rule.*, schedule.*, ...)
        * ``task``: per-task results (``task.done`` / ``task.failed``), only when ``tasks`` is true
        * ``reset``: the client fell behind; the stream closes and a reconnect resumes it

        Stored events are replayed only when ``after_id`` is given (or a reconnecting client
        sends ``Last-Event-ID``): this feed spans every job, so its full history can be large.
        """
        sub = self.engine.bus.subscribe(job_id=job_id, group_id=group_id, notifications=tasks)
        cursor = Cursor(after_id or 0)
        try:
            yield message("snapshot", {"jobs": self.snapshot(job_id, group_id)})
            if after_id is not None:
                async for msg in self._replay(cursor, job_id=job_id, group_id=group_id):
                    yield msg
            loop = asyncio.get_running_loop()
            next_snapshot = loop.time() + snapshot_every
            while not await disconnected():
                if sub.overflowed:
                    yield _reset(cursor)
                    return
                try:
                    channel, event = await asyncio.wait_for(
                        sub.queue.get(), max(0.0, next_snapshot - loop.time())
                    )
                    if channel == events.TASK:
                        yield message("task", event)
                    elif cursor.accept(event):
                        yield message("event", event, event["id"])
                except TimeoutError:
                    pass
                if loop.time() >= next_snapshot:  # also under a steady flow of task messages
                    yield message("snapshot", {"jobs": self.snapshot(job_id, group_id)})
                    next_snapshot = loop.time() + snapshot_every
        finally:
            self.engine.bus.unsubscribe(sub)
