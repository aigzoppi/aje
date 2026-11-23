"""Persistence on PostgreSQL with asyncpg: Pydantic row models and a pooled ``Store``.

Each table has a row model (a Pydantic ``Row`` naming its table in ``__tablename__``);
``Store`` maps rows to SQL with a few generic helpers on an asyncpg connection pool.
The pool size comes from ``AJE_DB_POOL_SIZE`` (``Settings.db_pool_size``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import contextlib
import datetime
import json
import re
from typing import Any, ClassVar, TypeVar

import asyncpg
import pydantic

from aje.infrastructure.persistence import migrations


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def ts(epoch: float | None) -> datetime.datetime | None:
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC) if epoch else None


# ---------------------------------------------------------------------------- rows


class Row(pydantic.BaseModel):
    """One table row. Fields are the table's columns, in order.

    ``__serial__`` marks tables whose integer ``id`` the database assigns (SERIAL): such rows
    are created with ``id=None`` and ``Store.add`` fills it in.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    __tablename__: ClassVar[str]
    __serial__: ClassVar[bool] = False


class GroupRow(Row):
    __tablename__: ClassVar[str] = "groups"
    id: str
    name: str
    status: str = "running"
    management: str = "active"
    max_workers: int | None = None
    contract: dict = pydantic.Field(default_factory=dict)
    created_at: datetime.datetime = pydantic.Field(default_factory=utcnow)
    updated_at: datetime.datetime = pydantic.Field(default_factory=utcnow)


class JobRow(Row):
    """A job: its spec, contract and last persisted state."""

    __tablename__: ClassVar[str] = "jobs"
    id: str
    group_id: str | None = None
    name: str
    pattern: str
    status: str
    management: str
    workers: int
    partitions: int | None = None
    closed: bool = False
    spec: dict
    contract: dict = pydantic.Field(default_factory=dict)
    message: str | None = None
    created_at: datetime.datetime = pydantic.Field(default_factory=utcnow)
    updated_at: datetime.datetime = pydantic.Field(default_factory=utcnow)
    finished_at: datetime.datetime | None = None
    counts: dict | None = None  # final counters, stored when the job finishes


class TaskRow(Row):
    """One farm task or data-parallel partition, and its outcome."""

    __tablename__: ClassVar[str] = "tasks"
    id: str
    job_id: str
    batch_id: str | None = None
    partition: int | None = None
    status: str = "queued"
    attempts: int = 1
    payload: Any = None
    result: Any = None
    error: str | None = None
    worker: str | None = None
    submitted_at: datetime.datetime = pydantic.Field(default_factory=utcnow)
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None


class BatchRow(Row):
    """A data-parallel batch and its gathered result."""

    __tablename__: ClassVar[str] = "batches"
    id: str
    job_id: str
    status: str = "running"
    size: int
    partitions: int
    completed_partitions: int = 0
    result: Any = None
    error: str | None = None
    created_at: datetime.datetime = pydantic.Field(default_factory=utcnow)
    finished_at: datetime.datetime | None = None
    duration: float | None = None


class EventRow(Row):
    __tablename__: ClassVar[str] = "events"
    __serial__: ClassVar[bool] = True
    id: int | None = None
    ts: datetime.datetime = pydantic.Field(default_factory=utcnow)
    job_id: str | None = None
    group_id: str | None = None
    kind: str
    message: str
    data: dict | None = None


class ScheduleRow(Row):
    """A cron schedule and the template of the jobs it starts."""

    __tablename__: ClassVar[str] = "schedules"
    id: str
    name: str
    cron: str
    timezone: str = "UTC"
    status: str = "active"  # active | suspended
    concurrency_policy: str = "forbid"
    starting_deadline_seconds: int | None = None
    group_id: str | None = None
    job: dict  # JobTemplate
    next_run_at: datetime.datetime | None = None
    last_run_at: datetime.datetime | None = None
    last_job_id: str | None = None
    created_at: datetime.datetime = pydantic.Field(default_factory=utcnow)
    updated_at: datetime.datetime = pydantic.Field(default_factory=utcnow)


class ScheduleRunRow(Row):
    __tablename__: ClassVar[str] = "schedule_runs"
    __serial__: ClassVar[bool] = True
    id: int | None = None
    schedule_id: str
    scheduled_at: datetime.datetime
    created_at: datetime.datetime = pydantic.Field(default_factory=utcnow)
    trigger: str  # cron | catch-up | manual
    status: str  # started | skipped | failed
    job_id: str | None = None
    message: str | None = None


ROWS: tuple[type[Row], ...] = (GroupRow, JobRow, TaskRow, BatchRow, EventRow, ScheduleRow, ScheduleRunRow)

R = TypeVar("R", bound=Row)


# ---------------------------------------------------------------------------- connections


def asyncpg_dsn(url: str) -> str:
    """Returns the ``postgresql://`` DSN asyncpg expects.

    Driver-qualified URLs (``postgresql+asyncpg://``) from older configurations are
    accepted and normalised.

    Args:
        url: A PostgreSQL URL.

    Returns:
        The DSN.

    Raises:
        ValueError: Not a PostgreSQL URL.
    """
    m = re.match(r"^postgres(?:ql)?(?:\+\w+)?://", url)
    if not m:
        raise ValueError(
            "AJE_DATABASE_URL must be a PostgreSQL URL (postgresql://user:password@host:port/db)"
        )
    return "postgresql://" + url[m.end() :]


async def init_connection(conn: asyncpg.Connection) -> None:
    """Decode/encode JSON columns as Python objects (asyncpg passes them as text by default)."""
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(type_name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def connect(url: str) -> asyncpg.Connection:
    """A single connection outside any pool (migrations, tools)."""
    conn = await asyncpg.connect(asyncpg_dsn(url))
    await init_connection(conn)
    return conn


def _q(identifier: str) -> str:
    return f'"{identifier}"'


def _check_columns(model: type[Row], names) -> None:
    unknown = set(names) - set(model.model_fields)
    if unknown:
        columns = ", ".join(sorted(unknown))
        raise ValueError(f"{model.__tablename__} has no column(s) {columns}")


def _where(model: type[Row], where: Mapping[str, Any], args: list) -> str:
    """WHERE clause from column -> value; a list/tuple/set means "any of", None means IS NULL."""
    _check_columns(model, where)
    parts = []
    for col, value in where.items():
        if value is None:
            parts.append(f"{_q(col)} IS NULL")
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            args.append(list(value))
            parts.append(f"{_q(col)} = ANY(${len(args)})")
        else:
            args.append(value)
            parts.append(f"{_q(col)} = ${len(args)}")
    return ("WHERE " + " AND ".join(parts)) if parts else ""


def _insert_sql(model: type[Row], cols: list[str], returning_id: bool = False) -> str:
    names = ", ".join(map(_q, cols))
    params = ", ".join(f"${n}" for n in range(1, len(cols) + 1))
    sql = f"INSERT INTO {_q(model.__tablename__)} ({names}) VALUES ({params})"
    return sql + " RETURNING id" if returning_id else sql


def _affected(status: str) -> int:
    return int(status.rsplit(" ", 1)[-1])


# ---------------------------------------------------------------------------- store


class Store:
    """Rows in PostgreSQL through an asyncpg connection pool.

    Every operation takes an optional ``conn`` (from ``transaction()``) to join a
    transaction; without it, a pooled connection is used for that operation alone.
    """

    def __init__(self, url: str, *, pool_size: int = 10, auto_create: bool = True):
        """Configures the store; ``init`` opens the pool.

        Args:
            url: PostgreSQL URL (AJE_DATABASE_URL).
            pool_size: Connections in the pool (AJE_DB_POOL_SIZE).
            auto_create: Apply the migrations at start (dev / tests). Deployments run them
                separately (``python -m aje.migrate``) and turn it off.

        Raises:
            ValueError: Not a PostgreSQL URL, or a pool size below 1.
        """
        if pool_size < 1:
            raise ValueError("the database pool size must be at least 1")
        self.dsn = asyncpg_dsn(url)
        self.pool_size = pool_size
        self.auto_create = auto_create
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("the store is not started (call Store.init first)")
        return self._pool

    async def init(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.dsn, min_size=self.pool_size, max_size=self.pool_size, init=init_connection
            )
        if self.auto_create:
            async with self.pool.acquire() as conn:
                await migrations.upgrade_connection(conn)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """A pooled connection inside a transaction; pass it as ``conn=`` to group operations."""
        async with self.pool.acquire() as conn, conn.transaction():
            yield conn

    @contextlib.asynccontextmanager
    async def _connection(self, conn: asyncpg.Connection | None) -> AsyncIterator[asyncpg.Connection]:
        if conn is not None:
            yield conn
        else:
            async with self.pool.acquire() as c:
                yield c

    # ---- writes
    async def add(self, *rows: Row, conn: asyncpg.Connection | None = None) -> None:
        """Insert rows in one transaction; SERIAL ids are written back onto the rows."""
        if not rows:
            return
        async with self._connection(conn) as c, c.transaction():
            i = 0
            while i < len(rows):
                model = type(rows[i])
                if model.__serial__ and rows[i].id is None:
                    row = rows[i]
                    cols = [k for k in model.model_fields if k != "id"]
                    sql = _insert_sql(model, cols, returning_id=True)
                    row.id = await c.fetchval(sql, *(getattr(row, k) for k in cols))
                    i += 1
                    continue
                j = i + 1  # a run of rows of the same table: one executemany
                while (
                    j < len(rows)
                    and isinstance(rows[j], model)
                    and not (model.__serial__ and rows[j].id is None)
                ):
                    j += 1
                cols = list(model.model_fields)
                await c.executemany(
                    _insert_sql(model, cols), [tuple(getattr(r, k) for k in cols) for r in rows[i:j]]
                )
                i = j

    async def update(
        self, model: type[Row], key: Any, *, conn: asyncpg.Connection | None = None, **values: Any
    ) -> int:
        return await self.update_where(model, {"id": key}, conn=conn, **values)

    async def update_where(
        self,
        model: type[Row],
        where: Mapping[str, Any],
        *,
        conn: asyncpg.Connection | None = None,
        **values: Any,
    ) -> int:
        """UPDATE matching rows; ``updated_at`` is refreshed when the table has it. Returns the row count."""
        if "updated_at" in model.model_fields and "updated_at" not in values:
            values["updated_at"] = utcnow()
        _check_columns(model, values)
        args = list(values.values())
        sets = ", ".join(f"{_q(col)} = ${n}" for n, col in enumerate(values, start=1))
        sql = f"UPDATE {_q(model.__tablename__)} SET {sets} {_where(model, where, args)}"
        async with self._connection(conn) as c:
            return _affected(await c.execute(sql, *args))

    async def delete(
        self, model: type[Row], where: Mapping[str, Any], *, conn: asyncpg.Connection | None = None
    ) -> int:
        args: list = []
        sql = f"DELETE FROM {_q(model.__tablename__)} {_where(model, where, args)}"
        async with self._connection(conn) as c:
            return _affected(await c.execute(sql, *args))

    # ---- reads
    async def get(self, model: type[R], key: Any, *, conn: asyncpg.Connection | None = None) -> R | None:
        rows = await self.find(model, {"id": key}, conn=conn)
        return rows[0] if rows else None

    async def find(
        self,
        model: type[R],
        where: Mapping[str, Any] | None = None,
        *,
        order_by: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> list[R]:
        """Reads the rows matching ``where``.

        Args:
            model: The table's row model.
            where: Column -> value (see ``_where``: lists mean any-of, None means IS NULL).
            order_by: Trusted SQL, e.g. ``"created_at DESC"``.
            limit: Maximum rows.
            offset: Rows to skip.
            conn: A connection from ``transaction()``, to read inside it.

        Returns:
            The rows, as models.
        """
        args: list = []
        clause = _where(model, where or {}, args)
        if order_by:
            clause += f" ORDER BY {order_by}"
        if limit is not None:
            args.append(limit)
            clause += f" LIMIT ${len(args)}"
        if offset:
            args.append(offset)
            clause += f" OFFSET ${len(args)}"
        return await self.query(model, clause, *args, conn=conn)

    async def query(
        self, model: type[R], clause: str, *args: Any, conn: asyncpg.Connection | None = None
    ) -> list[R]:
        """``SELECT <every column> FROM <table> <clause>`` with ``$n`` parameters."""
        cols = ", ".join(map(_q, model.model_fields))
        sql = f"SELECT {cols} FROM {_q(model.__tablename__)} {clause}"
        async with self._connection(conn) as c:
            records = await c.fetch(sql, *args)
        return [model.model_validate(dict(r)) for r in records]

    async def fetchrow(
        self, sql: str, *args: Any, conn: asyncpg.Connection | None = None
    ) -> dict[str, Any] | None:
        """Runs a read-only query (aggregates, reports) and returns its first row, if any."""
        async with self._connection(conn) as c:
            record = await c.fetchrow(sql, *args)
        return dict(record) if record is not None else None
