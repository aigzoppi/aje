"""PostgreSQL (asyncpg) implementations of the repository ports (``aje.application.ports``)."""

from __future__ import annotations

from typing import Any

from aje.infrastructure.persistence import db


def _given(**filters: Any) -> dict[str, Any]:
    """Only the filters the caller set (empty values mean "no filter", as in the API)."""
    return {k: v for k, v in filters.items() if v}


class SqlJobRepository:
    """JobRepository on the Store."""

    def __init__(self, store: db.Store):
        self.store = store

    async def get(self, job_id: str) -> db.JobRow | None:
        return await self.store.get(db.JobRow, job_id)

    async def list(
        self,
        *,
        status: str | None = None,
        pattern: str | None = None,
        group_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[db.JobRow]:
        return await self.store.find(
            db.JobRow,
            _given(status=status, pattern=pattern, group_id=group_id),
            order_by="created_at DESC",
            limit=limit,
            offset=offset,
        )

    async def list_by_group(self, group_id: str) -> list[db.JobRow]:
        return await self.store.find(db.JobRow, {"group_id": group_id}, order_by="created_at")

    async def statuses(self, job_ids: list[str]) -> dict[str, str]:
        if not job_ids:
            return {}
        rows = await self.store.find(db.JobRow, {"id": job_ids})
        return {r.id: r.status for r in rows}

    async def counts(self, job_id: str, pattern: str) -> dict[str, int]:
        """Counters of a job rebuilt from its tasks (and batches), in the live counters' keys.

        Used for jobs that finished before their final counters were stored on the job row.
        ``retried`` counts extra attempts, so it also includes orphan re-dispatches.
        """
        row = await self.store.fetchrow(
            "SELECT count(*) AS submitted,"
            " count(*) FILTER (WHERE status = 'done') AS completed,"
            " count(*) FILTER (WHERE status = 'failed') AS failed,"
            " coalesce(sum(attempts - 1), 0) AS retried,"
            " count(*) FILTER (WHERE status = 'queued') AS outstanding"
            " FROM tasks WHERE job_id = $1",
            job_id,
        )
        counts = {k: int(v) for k, v in (row or {}).items()}
        if pattern == "data_parallel":
            batches = await self.store.fetchrow(
                "SELECT count(*) FILTER (WHERE status = 'running') AS batches_pending,"
                " count(*) FILTER (WHERE status = 'done') AS batches_done,"
                " count(*) FILTER (WHERE status = 'failed') AS batches_failed"
                " FROM batches WHERE job_id = $1",
                job_id,
            )
            counts.update({k: int(v) for k, v in (batches or {}).items()})
        return counts


class SqlTaskRepository:
    def __init__(self, store: db.Store):
        self.store = store

    async def get(self, task_id: str) -> db.TaskRow | None:
        return await self.store.get(db.TaskRow, task_id)

    async def list(
        self, job_id: str, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[db.TaskRow]:
        return await self.store.find(
            db.TaskRow,
            {"job_id": job_id, **_given(status=status)},
            order_by="submitted_at",
            limit=limit,
            offset=offset,
        )


class SqlBatchRepository:
    def __init__(self, store: db.Store):
        self.store = store

    async def get(self, batch_id: str) -> db.BatchRow | None:
        return await self.store.get(db.BatchRow, batch_id)

    async def list(self, job_id: str, *, limit: int = 100, offset: int = 0) -> list[db.BatchRow]:
        return await self.store.find(
            db.BatchRow, {"job_id": job_id}, order_by="created_at", limit=limit, offset=offset
        )


class SqlEventRepository:
    """EventRepository on the Store."""

    def __init__(self, store: db.Store):
        self.store = store

    async def list(
        self,
        *,
        job_id: str | None = None,
        group_id: str | None = None,
        kind: str | None = None,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[db.EventRow]:
        """Lists events after ``after_id``, oldest first.

        Args:
            job_id: Only events of this job.
            group_id: Only events of this group.
            kind: Only kinds starting with this prefix (``"job."`` selects every job event).
            after_id: Only events with a larger id (for polling).
            limit: Maximum events.

        Returns:
            The events.
        """
        args: list[Any] = [after_id]
        clause = "WHERE id > $1"
        for column, value in (("job_id", job_id), ("group_id", group_id)):
            if value:
                args.append(value)
                clause += f" AND {column} = ${len(args)}"
        if kind:  # prefix match: "job." selects every job event
            args.append(kind)
            clause += f" AND starts_with(kind, ${len(args)})"
        args.append(limit)
        clause += f" ORDER BY id LIMIT ${len(args)}"
        return await self.store.query(db.EventRow, clause, *args)


class SqlGroupRepository:
    def __init__(self, store: db.Store):
        self.store = store

    async def get(self, group_id: str) -> db.GroupRow | None:
        return await self.store.get(db.GroupRow, group_id)

    async def list(self, *, limit: int = 50) -> list[db.GroupRow]:
        return await self.store.find(db.GroupRow, order_by="created_at DESC", limit=limit)


class SqlScheduleRunRepository:
    def __init__(self, store: db.Store):
        self.store = store

    async def list(
        self, schedule_id: str, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[db.ScheduleRunRow]:
        return await self.store.find(
            db.ScheduleRunRow,
            {"schedule_id": schedule_id, **_given(status=status)},
            order_by="id DESC",
            limit=limit,
            offset=offset,
        )
