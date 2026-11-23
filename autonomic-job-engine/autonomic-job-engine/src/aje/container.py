"""Composition root: the only place that knows every concrete class.

``build_container(settings)`` wires infrastructure (store, broker, backend) into the
engine, the repositories into the services, and hands the result to the API, which
reaches the services through FastAPI dependencies (``aje.api.deps``). Tests build a
container around their own engine with ``Container(engine)``.
"""

from __future__ import annotations

from aje import config
from aje.application import services
from aje.application.ports import backend as backend_lib
from aje.application.ports import broker as broker_lib
from aje.application.runtime import engine as engine_lib
from aje.infrastructure.backends import registry
from aje.infrastructure.broker import amqp
from aje.infrastructure.persistence import db
from aje.infrastructure.persistence import repositories


def create_broker(settings: config.Settings) -> broker_lib.Broker:
    return amqp.AmqpBroker(settings.amqp_url)


def build_engine(
    settings: config.Settings,
    *,
    store: db.Store | None = None,
    broker: broker_lib.Broker | None = None,
    backend: backend_lib.Backend | None = None,
) -> engine_lib.AutonomicEngine:
    return engine_lib.AutonomicEngine(
        settings,
        store
        or db.Store(
            settings.database_url, pool_size=settings.db_pool_size, auto_create=settings.db_auto_create
        ),
        broker or create_broker(settings),
        backend or registry.create_backend(settings),
    )


class Container:
    """The services wired around one engine; what the HTTP app depends on."""

    def __init__(self, engine: engine_lib.AutonomicEngine):
        self.engine = engine
        self.settings: config.Settings = engine.settings
        store = engine.store
        job_repo = repositories.SqlJobRepository(store)
        event_repo = repositories.SqlEventRepository(store)
        group_repo = repositories.SqlGroupRepository(store)
        self.jobs = services.JobService(
            engine,
            engine.backend,
            job_repo,
            repositories.SqlTaskRepository(store),
            repositories.SqlBatchRepository(store),
            event_repo,
        )
        self.groups = services.GroupService(engine, group_repo, job_repo, event_repo, self.jobs)
        self.schedules = services.ScheduleService(
            engine, engine.scheduler, repositories.SqlScheduleRunRepository(store), job_repo
        )
        self.contracts = services.ContractService()
        self.streams = services.StreamService(engine, job_repo, group_repo, event_repo)

    async def start(self) -> None:
        await self.engine.start()

    async def stop(self) -> None:
        await self.engine.stop()


def build_container(settings: config.Settings) -> Container:
    return Container(build_engine(settings))
