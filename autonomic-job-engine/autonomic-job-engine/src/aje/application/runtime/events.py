"""Event bus: every management decision and lifecycle change is persisted and streamed.

Two kinds of messages go through the bus:
  * events (``emit``): persisted, then pushed to every matching subscriber
  * notifications (``notify``): pushed only, never stored, and only to subscribers that
    asked for them (``notifications=True``). Used for high-volume updates such as task
    results, which live dashboards want but the event log does not.

Subscribers receive ``(channel, message)`` pairs: channel ``event`` (an event, normally
with its database id) or ``task`` (a notification). A subscriber that falls behind is
marked ``overflowed`` instead of silently losing messages; its stream then ends and the
client resumes from the event log.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aje.infrastructure.persistence import db

log = logging.getLogger("aje.events")


EVENT = "event"
TASK = "task"


class Subscription:
    """One subscriber: a bounded queue of ``(channel, message)`` pairs plus its filter.

    Attributes:
        queue: Pending ``(EVENT | TASK, message)`` pairs.
        overflowed: True once a message could not be queued; nothing more is queued after that.
    """

    __slots__ = ("queue", "job_id", "group_id", "notifications", "overflowed")

    def __init__(self, job_id: str | None, group_id: str | None, notifications: bool, maxsize: int):
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=maxsize)
        self.job_id = job_id
        self.group_id = group_id
        self.notifications = notifications
        self.overflowed = False

    def matches(self, job_id: str | None, group_id: str | None) -> bool:
        return not ((self.job_id and self.job_id != job_id) or (self.group_id and self.group_id != group_id))

    def push(self, channel: str, message: dict[str, Any]) -> None:
        if self.overflowed:
            return
        if self.queue.full():  # slow consumer: stop here rather than lose messages silently
            self.overflowed = True
            return
        self.queue.put_nowait((channel, message))


class EventBus:
    """Persists events and fans them (and live notifications) out to subscribers."""

    def __init__(self, store: db.Store):
        self.store = store
        self._subs: set[Subscription] = set()

    async def emit(
        self,
        kind: str,
        message: str,
        *,
        job_id: str | None = None,
        group_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Persists an event and pushes it to every matching subscriber.

        A failure to persist is logged, never raised: events must not break management.

        Args:
            kind: Dotted event kind, e.g. ``job.scaled``.
            message: Human-readable description.
            job_id: The job the event is about, if any.
            group_id: The group the event is about, if any.
            data: Structured details.
        """
        row = db.EventRow(
            ts=db.utcnow(), job_id=job_id, group_id=group_id, kind=kind, message=message, data=data
        )
        try:
            await self.store.add(row)
        except Exception:  # pylint: disable=broad-exception-caught  # events must never break management
            log.exception("could not persist event")
        log.info("[%s] %s%s", kind, f"job={job_id} " if job_id else "", message)
        event = {
            "id": row.id,
            "ts": row.ts.isoformat(),
            "job_id": job_id,
            "group_id": group_id,
            "kind": kind,
            "message": message,
            "data": data,
        }
        for sub in list(self._subs):
            if sub.matches(job_id, group_id):
                sub.push(EVENT, event)

    def wants_notifications(self, job_id: str | None = None, group_id: str | None = None) -> bool:
        """Cheap check so producers can skip building notifications nobody reads."""
        return any(s.notifications and s.matches(job_id, group_id) for s in self._subs)

    def notify(
        self,
        kind: str,
        *,
        job_id: str | None = None,
        group_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Pushes a live, non-persisted message to subscribers that asked for notifications.

        Args:
            kind: Dotted notification kind, e.g. ``task.done``.
            job_id: The job the notification is about, if any.
            group_id: The group the notification is about, if any.
            data: The payload.
        """
        event = {
            "id": None,
            "ts": db.utcnow().isoformat(),
            "job_id": job_id,
            "group_id": group_id,
            "kind": kind,
            "message": "",
            "data": data,
        }
        for sub in list(self._subs):
            if sub.notifications and sub.matches(job_id, group_id):
                sub.push(TASK, event)

    def subscribe(
        self,
        job_id: str | None = None,
        group_id: str | None = None,
        notifications: bool = False,
        maxsize: int = 1000,
    ) -> Subscription:
        """Subscribes to events (and, with ``notifications``, task notifications).

        Args:
            job_id: Only messages about this job.
            group_id: Only messages about this group (including its member jobs).
            notifications: Also receive task notifications.
            maxsize: Queue bound; beyond it the subscription overflows.

        Returns:
            The subscription; pass it to ``unsubscribe`` when done.
        """
        sub = Subscription(job_id, group_id, notifications, maxsize)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)
