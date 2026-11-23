"""Apply the database migrations: ``python -m aje.migrate [revision]`` (default: head).

Uses AJE_DATABASE_URL like the engine. Docker compose runs it in the ``migrate`` service
before the API starts; the API then runs with AJE_DB_AUTO_CREATE=false.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aje import config
from aje.infrastructure.persistence import db
from aje.infrastructure.persistence import migrations

log = logging.getLogger("aje.migrate")


async def _with_connection(database_url: str, fn, *args):
    conn = await db.connect(database_url)
    try:
        return await fn(conn, *args)
    finally:
        await conn.close()


def upgrade(database_url: str, revision: str = "head") -> list[str]:
    return asyncio.run(_with_connection(database_url, migrations.upgrade_connection, revision))


def downgrade(database_url: str, revision: str = "base") -> list[str]:
    return asyncio.run(_with_connection(database_url, migrations.downgrade_connection, revision))


def current_revision(database_url: str) -> str | None:
    """The revision the database is at, or None before the first migration."""
    return asyncio.run(_with_connection(database_url, migrations.current_revision))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    target = sys.argv[1] if len(sys.argv) > 1 else "head"
    url = config.Settings().database_url
    log.info("migrating %s to %s", url.rsplit("@", maxsplit=1)[-1], target)
    applied = upgrade(url, target)
    log.info("applied %s", ", ".join(applied) if applied else "nothing (already up to date)")


if __name__ == "__main__":
    main()
