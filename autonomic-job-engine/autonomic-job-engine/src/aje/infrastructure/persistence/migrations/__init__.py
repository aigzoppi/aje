"""Schema migrations, applied with asyncpg. Run them with ``python -m aje.migrate``.

Each module in ``versions/`` defines ``revision``, ``down_revision`` (``None`` for the first)
and the SQL strings ``upgrade`` and ``downgrade``. Revisions form one chain; a migration run
happens in a single transaction (PostgreSQL DDL is transactional) under an advisory lock,
so concurrent starts cannot apply the same revision twice.

The current revision is kept in the ``alembic_version`` table: the engine used Alembic
before, and keeping its bookkeeping table lets existing databases carry on unchanged.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

import asyncpg
import pydantic

log = logging.getLogger("aje.migrate")

VERSION_TABLE = "alembic_version"
ADVISORY_LOCK = 0x414A45_0001  # "AJE" + 1: serialises migration runs

# Tables created by the baseline revision. A database that has all of them but no version
# table was built before the migrations existed; it is adopted by stamping the baseline.
BASELINE_REVISION = "0001"
BASELINE_TABLES = frozenset({"groups", "jobs", "tasks", "batches", "events", "schedules", "schedule_runs"})


class Migration(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    revision: str
    down_revision: str | None
    upgrade: str
    downgrade: str


def migrations() -> list[Migration]:
    """Every revision, oldest first."""
    package = importlib.import_module(f"{__name__}.versions")
    found = []
    for info in pkgutil.iter_modules(package.__path__):
        mod = importlib.import_module(f"{package.__name__}.{info.name}")
        found.append(
            Migration(
                revision=mod.revision,
                down_revision=mod.down_revision,
                upgrade=mod.upgrade,
                downgrade=mod.downgrade,
            )
        )
    by_parent = {m.down_revision: m for m in found}
    if len(by_parent) != len(found):
        raise RuntimeError("two migrations share the same down_revision (the chain branches)")
    chain, parent = [], None
    while parent in by_parent:
        m = by_parent.pop(parent)
        chain.append(m)
        parent = m.revision
    if by_parent:
        raise RuntimeError(
            "migrations not linked to the chain: " + ", ".join(m.revision for m in by_parent.values())
        )
    return chain


def _index(chain: list[Migration], revision: str) -> int:
    for i, m in enumerate(chain):
        if m.revision == revision:
            return i
    raise ValueError(f"unknown revision {revision!r}")


async def existing_tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    return {r["tablename"] for r in rows}


async def current_revision(conn: asyncpg.Connection) -> str | None:
    if VERSION_TABLE not in await existing_tables(conn):
        return None
    return await conn.fetchval(f"SELECT version_num FROM {VERSION_TABLE}")


async def _stamp(conn: asyncpg.Connection, revision: str | None) -> None:
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (version_num VARCHAR(32) NOT NULL, "
        f"CONSTRAINT {VERSION_TABLE}_pkc PRIMARY KEY (version_num))"
    )
    await conn.execute(f"DELETE FROM {VERSION_TABLE}")
    if revision is not None:
        await conn.execute(f"INSERT INTO {VERSION_TABLE} (version_num) VALUES ($1)", revision)


async def _adopt_unversioned_schema(conn: asyncpg.Connection) -> bool:
    """Stamp the baseline on a schema created without migrations. Returns True if it stamped."""
    tables = await existing_tables(conn)
    if VERSION_TABLE in tables:
        return False
    present = tables & BASELINE_TABLES
    if not present:
        return False  # empty database: the normal upgrade creates everything
    if present != BASELINE_TABLES:
        missing = ", ".join(sorted(BASELINE_TABLES - present))
        raise RuntimeError(
            f"database has part of the AJE schema without {VERSION_TABLE} (missing: {missing}); "
            "fix it by hand or drop the tables before migrating"
        )
    log.warning("schema exists without %s; stamping %s", VERSION_TABLE, BASELINE_REVISION)
    await _stamp(conn, BASELINE_REVISION)
    return True


async def upgrade_connection(conn: asyncpg.Connection, revision: str = "head") -> list[str]:
    """Apply the migrations up to ``revision`` (default: the newest). Returns the applied revisions."""
    chain = migrations()
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK)
        await _adopt_unversioned_schema(conn)
        current = await current_revision(conn)
        start = 0 if current is None else _index(chain, current) + 1
        stop = len(chain) if revision == "head" else _index(chain, revision) + 1
        applied = []
        for m in chain[start:stop]:
            log.info("upgrade %s -> %s", m.down_revision or "base", m.revision)
            await conn.execute(m.upgrade)
            await _stamp(conn, m.revision)
            applied.append(m.revision)
        return applied


async def downgrade_connection(conn: asyncpg.Connection, revision: str = "base") -> list[str]:
    """Reverts the migrations down to ``revision``.

    Args:
        conn: A connection to the database.
        revision: The revision to keep; ``base`` reverts everything.

    Returns:
        The reverted revisions, newest first.
    """
    chain = migrations()
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK)
        current = await current_revision(conn)
        if current is None:
            return []
        stop = -1 if revision == "base" else _index(chain, revision)
        reverted = []
        for m in reversed(chain[stop + 1 : _index(chain, current) + 1]):
            log.info("downgrade %s -> %s", m.revision, m.down_revision or "base")
            await conn.execute(m.downgrade)
            await _stamp(conn, m.down_revision)
            reverted.append(m.revision)
        return reverted
