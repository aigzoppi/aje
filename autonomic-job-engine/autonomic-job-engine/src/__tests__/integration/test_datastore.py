"""PostgreSQL data plane: workers read and write job data directly in PostgreSQL.

RabbitMQ only carries id ranges and row counts.
"""

import asyncio

import psycopg
import pytest

from aje_worker.examples import datastore
from common import api as api_lib
from common import specs

DATASTORE = {"input": "dataplane.points", "output": "dataplane.results"}


class DataPlane:
    """The job data the workers own: a ``dataplane`` schema with input points and results."""

    def __init__(self, dsn: str, rows: int = 1000):
        self.dsn = dsn
        self.rows = rows

    def create(self) -> None:
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS dataplane CASCADE")
            cur.execute("CREATE SCHEMA dataplane")
            cur.execute("CREATE TABLE dataplane.points (id bigint PRIMARY KEY, x double precision NOT NULL)")
            cur.execute(
                "CREATE TABLE dataplane.results (id bigint PRIMARY KEY, y double precision, worker text)"
            )
            cur.executemany(
                "INSERT INTO dataplane.points VALUES (%s, %s)", [(i, i / 10) for i in range(self.rows)]
            )

    async def results(self) -> list[tuple]:
        """(id, y, x, worker) for every result row."""

        def read():
            with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT r.id, r.y, p.x, r.worker FROM dataplane.results r "
                    "JOIN dataplane.points p USING (id)"
                )
                return cur.fetchall()

        return await asyncio.to_thread(read)


@pytest.fixture(name="dataplane")
def dataplane_fixture(database_url, monkeypatch) -> DataPlane:
    plane = DataPlane(database_url)
    plane.create()
    monkeypatch.setenv("AJE_DATASTORE_DSN", database_url)  # the worker environment holds the DSN, not the job
    return plane


class DataPlaneTest:
    async def test_workers_read_and_write_postgres_directly(self, probe, dataplane):
        m = await probe.submit(
            specs.farm(
                name="pg",
                handler="power_rows",
                workers=3,
                max_workers=3,
                management="passive",
                tasks=datastore.id_ranges(0, dataplane.rows, 100),
                close_when_done=True,
                jobcontext={"power": 3, "datastore": DATASTORE},
            )
        )
        await probe.succeeded(m, timeout=30)

        rows = await dataplane.results()
        assert len(rows) == dataplane.rows
        assert all(abs(y - x**3) < 1e-9 for _, y, x, _ in rows)
        assert all(w for *_, w in rows)  # every row records the worker that wrote it

        # the control plane only saw references and counts
        tasks = await probe.tasks(m)
        assert sorted(t.result["rows"] for t in tasks) == [100] * 10
        assert all(set(t.payload) == {"from", "to"} for t in tasks)

    @pytest.mark.usefixtures("dataplane")
    async def test_api_shows_the_context_and_refuses_credentials(self, api):
        body = specs.farm_body(
            "ctx", handler="power_rows", management="passive", jobcontext={"power": 2, "datastore": DATASTORE}
        )
        job = api_lib.expect(await api.jobs.create(body), 201)
        assert (
            job["jobcontext"]["power"] == 2 and job["jobcontext"]["datastore"]["input"] == DATASTORE["input"]
        )
        assert api_lib.expect(await api.jobs.get(job["id"]))["jobcontext"] == job["jobcontext"]

        leaked = {**body, "name": "leak", "jobcontext": {"db_password": "hunter2"}}
        r = await api.jobs.create(leaked)
        assert r.status_code == 422 and "credentials" in r.text
