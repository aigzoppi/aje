"""Store helpers that need no database: DSN handling, SQL building, the migration chain, settings."""

import itertools

import pytest

from aje import config
from aje.infrastructure.persistence import db
from aje.infrastructure.persistence import migrations


class DsnTest:
    @pytest.mark.parametrize(
        "url",
        ["postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db", "postgres://u:p@h:5432/db"],
    )
    def test_is_normalised_for_asyncpg(self, url):
        assert db.asyncpg_dsn(url) == "postgresql://u:p@h:5432/db"

    def test_non_postgres_urls_are_rejected(self):
        with pytest.raises(ValueError, match="PostgreSQL"):
            db.asyncpg_dsn("mysql://u:p@h:3306/db")


class WhereClauseTest:
    """The private SQL builder behind Store.find / update_where / delete."""

    # pylint: disable=protected-access

    def test_builds_parameters(self):
        args: list = []
        sql = db._where(db.TaskRow, {"job_id": "j", "status": ["queued", "done"], "batch_id": None}, args)
        assert sql == 'WHERE "job_id" = $1 AND "status" = ANY($2) AND "batch_id" IS NULL'
        assert args == ["j", ["queued", "done"]]

    def test_rejects_unknown_columns(self):
        with pytest.raises(ValueError, match="no column"):
            db._where(db.TaskRow, {"nope": 1}, [])


class PoolSizeTest:
    def test_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("AJE_DB_POOL_SIZE", "7")
        assert config.Settings().db_pool_size == 7

    @pytest.mark.parametrize("size", ["0", "-1"])
    def test_must_be_positive_in_settings(self, monkeypatch, size):
        monkeypatch.setenv("AJE_DB_POOL_SIZE", size)
        with pytest.raises(ValueError):
            config.Settings()

    def test_must_be_positive_in_the_store(self):
        with pytest.raises(ValueError):
            db.Store("postgresql://h/db", pool_size=0)


class MigrationChainTest:
    def test_is_a_single_linked_chain(self):
        chain = migrations.migrations()
        assert chain[0].down_revision is None
        assert all(b.down_revision == a.revision for a, b in itertools.pairwise(chain))
