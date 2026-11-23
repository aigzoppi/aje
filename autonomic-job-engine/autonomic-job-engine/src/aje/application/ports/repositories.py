"""Ports of the application layer: the read-side queries services need.

Services depend on these protocols, never on the database driver; ``aje.persistence`` provides
the SQL implementations and the container wires them in (dependency inversion).
"""

from __future__ import annotations

from typing import Protocol

from aje.infrastructure.persistence import db

JobRow = db.JobRow  # the row type JobRepository returns, for services' annotations


class JobRepository(Protocol):
    """Read access to stored jobs."""

    async def get(self, job_id: str) -> db.JobRow | None: ...
    async def list(
        self,
        *,
        status: str | None = None,
        pattern: str | None = None,
        group_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[db.JobRow]: ...
    async def list_by_group(self, group_id: str) -> list[db.JobRow]: ...
    async def statuses(self, job_ids: list[str]) -> dict[str, str]: ...
    async def counts(self, job_id: str, pattern: str) -> dict[str, int]: ...


class TaskRepository(Protocol):
    async def get(self, task_id: str) -> db.TaskRow | None: ...
    async def list(
        self, job_id: str, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[db.TaskRow]: ...


class BatchRepository(Protocol):
    async def get(self, batch_id: str) -> db.BatchRow | None: ...
    async def list(self, job_id: str, *, limit: int = 100, offset: int = 0) -> list[db.BatchRow]: ...


class EventRepository(Protocol):
    async def list(
        self,
        *,
        job_id: str | None = None,
        group_id: str | None = None,
        kind: str | None = None,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[db.EventRow]: ...


class GroupRepository(Protocol):
    async def get(self, group_id: str) -> db.GroupRow | None: ...
    async def list(self, *, limit: int = 50) -> list[db.GroupRow]: ...


class ScheduleRunRepository(Protocol):
    async def list(
        self, schedule_id: str, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[db.ScheduleRunRow]: ...
