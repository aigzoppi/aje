"""Build engines for integration tests (real broker and database, in-process AMQP workers)."""

from collections.abc import AsyncIterator
import contextlib

from aje import config
from aje import container
from aje.application.runtime import engine as engine_lib
from aje.infrastructure.backends import inprocess
from aje_worker import amqp
from common import handlers


def make_settings(database_url: str, amqp_url: str, **overrides) -> config.Settings:
    values = {
        "database_url": database_url,
        "amqp_url": amqp_url,
        "db_auto_create": False,
        "db_pool_size": 4,
        "monitor_interval": 0.1,
        "monitor_window": 5,
        "heartbeat_interval": 0.2,
        "group_interval": 0.1,
    }
    return config.Settings(**{**values, **overrides})


def make_engine(settings: config.Settings, heartbeat: float = 0.1) -> engine_lib.AutonomicEngine:
    backend = inprocess.InProcessBackend(
        lambda job_id: amqp.AmqpWorkerTransport(settings.amqp_url, job_id),
        handlers.HANDLERS,
        heartbeat=heartbeat,
    )
    return container.build_engine(settings, backend=backend)


@contextlib.asynccontextmanager
async def running_engine(
    settings: config.Settings, heartbeat: float = 0.1
) -> AsyncIterator[engine_lib.AutonomicEngine]:
    """A started engine, stopped on exit."""
    engine = make_engine(settings, heartbeat)
    await engine.start()
    try:
        yield engine
    finally:
        await engine.stop()
