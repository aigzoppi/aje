"""Integration fixtures: the real engine against PostgreSQL and RabbitMQ in Testcontainers.

Fixtures are layered, each one building on the previous:

    database_url, amqp_url    one PostgreSQL and one RabbitMQ container per session
    schema                    SchemaAdmin for the session database
    clean_schema              downgrade + upgrade before the test (the migrations run every time)
    settings                  engine settings pointing at the containers
    engine                    a started engine (in-process workers speaking AMQP)
    probe                     EngineProbe around ``engine``: submit, wait, read persisted rows
    client / api              raw httpx client / ApiClient on the HTTP app wrapping ``engine``
"""

import pytest
import pytest_asyncio

from aje import container as container_lib
from aje import main
from aje.infrastructure.persistence import db
from common import api as api_lib
from common import containers
from common import engine as engine_lib
from common import probe as probe_lib
from common import schema as schema_lib


def pytest_collection_modifyitems(items):
    for item in items:
        if "integration" in item.nodeid.split("/"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(name="database_url", scope="session")
def database_url_fixture():
    with containers.postgres_container() as url:
        yield url


@pytest.fixture(name="amqp_url", scope="session")
def amqp_url_fixture():
    with containers.rabbitmq_container() as url:
        yield url


@pytest.fixture(name="schema", scope="session")
def schema_fixture(database_url) -> schema_lib.SchemaAdmin:
    return schema_lib.SchemaAdmin(database_url)


@pytest_asyncio.fixture(name="clean_schema")
async def clean_schema_fixture(schema) -> schema_lib.SchemaAdmin:
    await schema.reset()
    return schema


@pytest_asyncio.fixture(name="store")
async def store_fixture(clean_schema, database_url):
    """A started Store (pool of 2) on a freshly migrated schema, without an engine."""
    del clean_schema  # Unused: requested so every test starts from a rebuilt schema.
    s = db.Store(database_url, pool_size=2, auto_create=False)
    await s.init()
    yield s
    await s.close()


@pytest.fixture(name="settings")
def settings_fixture(database_url, amqp_url):
    return engine_lib.make_settings(database_url, amqp_url)


@pytest_asyncio.fixture(name="engine")
async def engine_fixture(settings, clean_schema):
    del clean_schema  # Unused: requested so every test starts from a rebuilt schema.
    async with engine_lib.running_engine(settings) as eng:
        yield eng


@pytest.fixture(name="services")
def services_fixture(engine) -> container_lib.Container:
    """The application services wired around ``engine`` (what the HTTP layer calls)."""
    return container_lib.Container(engine)


@pytest.fixture(name="probe")
def probe_fixture(engine) -> probe_lib.EngineProbe:
    return probe_lib.EngineProbe(engine)


@pytest_asyncio.fixture(name="client")
async def client_fixture(engine):
    async with api_lib.asgi_client(main.create_app(engine)) as c:
        yield c


@pytest.fixture(name="api")
def api_fixture(client) -> api_lib.ApiClient:
    return api_lib.ApiClient(client)
