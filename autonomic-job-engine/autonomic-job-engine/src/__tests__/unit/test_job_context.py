"""Job context: validation, transport to workers, and the PostgreSQL data store."""

import json

import pydantic
import pytest

from aje.application import schemas
from aje_worker import context as context_lib
from aje_worker import protocol
from aje_worker import runtime
from aje_worker.examples import datastore
from common import fakes
from common import kubernetes
from common import specs

JOB = specs.farm_body("j", handler="x")
STORE = {"input": "dataplane.points", "output": "results", "dsn_env": "PG_DSN", "dsn_secret": {"name": "pg"}}


def job_with(context: dict) -> schemas.JobCreate:
    return schemas.JobCreate(**JOB, jobcontext=context)


class JobContextTest:
    def test_arbitrary_parameters_round_trip(self):
        job = job_with({"power": 3, "model": {"name": "v2", "layers": [1, 2]}, "datastore": STORE})
        assert job.jobcontext.get("power") == 3 and job.jobcontext.parameters["model"]["layers"] == [1, 2]
        assert isinstance(job.jobcontext.datastore, context_lib.PostgresDataStore)
        again = context_lib.JobContext.model_validate(job.model_dump(mode="json")["jobcontext"])
        assert again == job.jobcontext
        assert context_lib.JobContext.from_env({protocol.ENV_JOB_CONTEXT: again.to_env()}) == again

    def test_default_is_empty(self):
        job = schemas.JobCreate(**JOB)
        assert job.jobcontext.parameters == {} and job.jobcontext.datastore is None
        assert context_lib.JobContext.from_env({}) == context_lib.JobContext()

    @pytest.mark.parametrize(
        "context, message",
        [
            ({"datastore": {**STORE, "input": "points; drop table x"}}, "not a table name"),
            ({"datastore": {**STORE, "password": "x"}}, "Extra inputs are not permitted"),
            ({"datastore": {**STORE, "dsn_env": "1-bad"}}, "environment variable"),
            ({"db": {"password": "hunter2"}}, "credentials"),
            ({"api_token": "abc"}, "credentials"),
            ({"blob": "x" * 70_000}, "larger than"),
        ],
    )
    def test_invalid_contexts_are_rejected(self, context, message):
        with pytest.raises(pydantic.ValidationError, match=message):
            job_with(context)


class PostgresDataStoreTest:
    @pytest.fixture(name="store")
    def store_fixture(self) -> context_lib.PostgresDataStore:
        return context_lib.PostgresDataStore(**STORE)

    def test_dsn_must_be_in_the_environment(self, store, monkeypatch):
        monkeypatch.delenv("PG_DSN", raising=False)
        with pytest.raises(RuntimeError, match="PG_DSN"):
            store.dsn()

    def test_dsn_is_normalised(self, store, monkeypatch):
        monkeypatch.setenv("PG_DSN", "postgresql+asyncpg://u:p@h/db")
        assert store.dsn() == "postgresql://u:p@h/db"

    def test_tables_are_quoted_identifiers(self, store):
        assert store.table("input").as_string(None) == '"dataplane"."points"'

    def test_id_ranges(self):
        assert datastore.id_ranges(0, 25, 10) == [
            {"from": 0, "to": 10},
            {"from": 10, "to": 20},
            {"from": 20, "to": 25},
        ]


class KubernetesWorkersTest:
    def test_get_the_context_and_the_secret(self):
        env = kubernetes.container_env(
            kubernetes.manifest(specs.worker_spec(job_context={"power": 2, "datastore": STORE}))
        )
        assert json.loads(env[protocol.ENV_JOB_CONTEXT]["value"])["power"] == 2
        assert env["PG_DSN"]["valueFrom"]["secretKeyRef"] == {"name": "pg", "key": "dsn"}

    def test_without_a_context_get_no_variable(self):
        assert protocol.ENV_JOB_CONTEXT not in kubernetes.container_env(
            kubernetes.manifest(specs.worker_spec())
        )


class WorkerHandlersTest:
    async def test_receive_the_job_context(self):
        seen = []

        def handler(payload, ctx):
            seen.append(ctx.job_context.get("power"))
            return payload ** ctx.job_context.get("power")

        async def ack():
            pass

        transport = fakes.FakeResultTransport()
        worker = runtime.Worker(
            handler, transport, job_id="j", worker_id="w", job_context=context_lib.JobContext(power=3)
        )
        await worker._on_task(  # pylint: disable=protected-access  # one delivery, no broker
            {"task_id": "t", "attempt": 1, "payload": 2}, ack, ack
        )
        assert seen == [3] and transport.results[0]["result"] == 8
