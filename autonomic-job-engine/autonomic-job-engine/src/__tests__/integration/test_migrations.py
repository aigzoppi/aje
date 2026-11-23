"""The migrations produce exactly the schema the row models describe, and Store maps rows faithfully."""

from typing import Any, get_args

import pytest
import pytest_asyncio

from aje.infrastructure.persistence import db
from aje.infrastructure.persistence import migrations

HEAD = migrations.migrations()[-1].revision


def allows_none(annotation) -> bool:
    return annotation is Any or type(None) in get_args(annotation)


class MigrationsTest:
    async def test_migrated_schema_matches_models(self, clean_schema):
        columns = await clean_schema.columns()
        for model in db.ROWS:
            table = columns.get(model.__tablename__)
            assert table is not None, f"no table {model.__tablename__}"
            assert set(table) == set(model.model_fields), model.__tablename__
            for name, field in model.model_fields.items():
                if name == "id" and model.__serial__:
                    continue  # None until the database assigns it
                assert allows_none(field.annotation) == table[name], (
                    f"{model.__tablename__}.{name} nullability"
                )

    async def test_downgrade_removes_everything_and_upgrade_restores_it(self, clean_schema):
        await clean_schema.downgrade()
        assert await clean_schema.tables() <= {migrations.VERSION_TABLE}
        assert await clean_schema.revision() is None
        await clean_schema.upgrade()
        assert {m.__tablename__ for m in db.ROWS} <= await clean_schema.tables()
        assert await clean_schema.revision() == HEAD

    async def test_upgrade_is_idempotent(self, clean_schema):
        assert await clean_schema.upgrade() == []


class UnversionedSchemasTest:
    """Databases built before the migrations existed: tables present, version table missing."""

    async def test_complete_schema_is_adopted(self, clean_schema):
        await clean_schema.execute(f"DROP TABLE {migrations.VERSION_TABLE}")
        await clean_schema.upgrade()
        assert await clean_schema.revision() == HEAD

    async def test_partial_schema_is_refused(self, clean_schema):
        await clean_schema.downgrade()
        await clean_schema.execute(f"DROP TABLE IF EXISTS {migrations.VERSION_TABLE}")
        await clean_schema.execute("CREATE TABLE groups (id VARCHAR(16) PRIMARY KEY)")
        try:
            with pytest.raises(RuntimeError, match="part of the AJE schema"):
                await clean_schema.upgrade()
        finally:
            await clean_schema.execute("DROP TABLE groups")  # leave it clean for the next test


@pytest.fixture(name="job_row")
def job_row_fixture() -> db.JobRow:
    return db.JobRow(
        id="j1",
        name="n",
        pattern="farm",
        status="running",
        management="passive",
        workers=1,
        spec={"min_workers": 1, "max_workers": 2},
    )


@pytest.fixture(name="task_rows")
def task_rows_fixture() -> list[db.TaskRow]:
    return [db.TaskRow(id=f"t{i}", job_id="j1", payload={"i": i, "xs": [i, None]}) for i in range(3)]


@pytest_asyncio.fixture(name="stored")
async def stored_fixture(store, job_row, task_rows):
    await store.add(job_row, *task_rows)
    return store


class StoreTest:
    async def test_pool_has_the_configured_size(self, store):
        assert store.pool.get_size() == 2

    async def test_rows_round_trip_with_json_and_timestamps(self, stored, job_row):
        assert await stored.get(db.JobRow, "j1") == job_row

    async def test_serial_ids_are_written_back(self, stored):
        event = db.EventRow(kind="job.started", message="go", job_id="j1", data={"k": "v"})
        await stored.add(event)
        assert event.id is not None and (await stored.find(db.EventRow, {"id": event.id}))[0].data == {
            "k": "v"
        }

    async def test_find_matches_any_of_a_list(self, stored):
        found = await stored.find(db.TaskRow, {"id": ["t0", "t2"]}, order_by="id")
        assert [t.payload for t in found] == [{"i": 0, "xs": [0, None]}, {"i": 2, "xs": [2, None]}]

    async def test_update_where_returns_the_row_count(self, stored):
        assert (
            await stored.update_where(db.TaskRow, {"job_id": "j1", "status": "queued"}, status="cancelled")
            == 3
        )
        assert len(await stored.find(db.TaskRow, {"status": "cancelled"})) == 3

    async def test_fetchrow_returns_the_first_row(self, stored):
        row = await stored.fetchrow("SELECT count(*) AS n FROM tasks WHERE job_id = $1", "j1")
        assert row == {"n": 3}
        assert await stored.fetchrow("SELECT id FROM tasks WHERE job_id = $1", "nope") is None

    async def test_update_refreshes_updated_at(self, stored, job_row):
        assert await stored.update(db.JobRow, "j1", workers=3) == 1
        updated = await stored.get(db.JobRow, "j1")
        assert updated.workers == 3 and updated.updated_at > job_row.updated_at

    async def test_delete(self, stored):
        assert await stored.delete(db.TaskRow, {"job_id": "j1"}) == 3
        assert await stored.find(db.TaskRow) == []
