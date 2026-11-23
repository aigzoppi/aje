"""SchemaAdmin: migrate and inspect the test database through the public migration API."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from aje import migrate
from aje.infrastructure.persistence import db
from aje.infrastructure.persistence import migrations


class SchemaAdmin:
    """Migrates and inspects one database through the public migration API."""

    def __init__(self, database_url: str):
        self.database_url = database_url

    # ---- migrations (``aje.migrate`` is synchronous, like the CLI: run it off the event loop)
    async def upgrade(self, revision: str = "head") -> list[str]:
        return await asyncio.to_thread(migrate.upgrade, self.database_url, revision)

    async def downgrade(self, revision: str = "base") -> list[str]:
        return await asyncio.to_thread(migrate.downgrade, self.database_url, revision)

    async def revision(self) -> str | None:
        return await asyncio.to_thread(migrate.current_revision, self.database_url)

    async def reset(self) -> None:
        """An empty, fully migrated schema."""
        await self.downgrade()
        await self.upgrade()

    # ---- inspection and raw SQL
    async def run(self, fn: Callable[[asyncpg.Connection], Awaitable[Any]]) -> Any:
        conn = await db.connect(self.database_url)
        try:
            return await fn(conn)
        finally:
            await conn.close()

    async def execute(self, sql: str) -> None:
        await self.run(lambda conn: conn.execute(sql))

    async def tables(self) -> set[str]:
        return await self.run(migrations.existing_tables)

    async def columns(self) -> dict[str, dict[str, bool]]:
        """Table -> column -> nullable, for the current schema."""
        rows = await self.run(
            lambda conn: conn.fetch(
                "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = current_schema()"
            )
        )
        out: dict[str, dict[str, bool]] = {}
        for r in rows:
            out.setdefault(r["table_name"], {})[r["column_name"]] = r["is_nullable"] == "YES"
        return out
