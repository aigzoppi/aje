"""Real infrastructure for integration tests, started with Testcontainers (Docker required)."""

from collections.abc import Iterator
import contextlib

try:
    from testcontainers.community import postgres
    from testcontainers.community import rabbitmq
except ImportError:  # testcontainers < 4.13
    from testcontainers import postgres
    from testcontainers import rabbitmq

POSTGRES_IMAGE = "postgres:16-alpine"
RABBITMQ_IMAGE = "rabbitmq:3.13-management"


@contextlib.contextmanager
def postgres_container() -> Iterator[str]:
    """Yields a PostgreSQL URL (``postgresql://...``) usable by asyncpg and psycopg."""
    with postgres.PostgresContainer(
        POSTGRES_IMAGE, username="aje", password="aje", dbname="aje", driver=None
    ) as pg:
        yield pg.get_connection_url()


@contextlib.contextmanager
def rabbitmq_container() -> Iterator[str]:
    """Yields an AMQP URL (``amqp://guest:guest@host:port/``)."""
    with rabbitmq.RabbitMqContainer(RABBITMQ_IMAGE) as mq:
        yield f"amqp://guest:guest@{mq.get_container_host_ip()}:{mq.get_exposed_port(5672)}/"
