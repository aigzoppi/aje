"""FastAPI application: submit, control and monitor autonomic jobs.

The app is only an adapter: it receives a service container (or a factory for one) from
the composition root (``aje.main``), exposes the services through dependencies and maps
domain errors to HTTP codes. It never decides which concrete classes are used.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import logging

import fastapi
from fastapi import responses
from fastapi.middleware import cors
import prometheus_client

import aje
from aje import config
from aje.domain import dsl
from aje.interfaces.http import contracts
from aje.interfaces.http import deps
from aje.interfaces.http import errors
from aje.interfaces.http import groups
from aje.interfaces.http import internal
from aje.interfaces.http import jobs
from aje.interfaces.http import schedules

DESCRIPTION = (
    "Submit, control and monitor self-managing jobs on Kubernetes. "
    "Jobs follow a parallel pattern (farm or data_parallel) and carry a contract written in a "
    "Drools-like rule language; a manager per job evaluates it continuously and adapts the "
    "job's non-functional knobs (workers, partitions, lifecycle). Groups add a second management "
    "level with a shared worker budget. Schedules launch jobs from a template on a cron expression."
)


def create_app(
    settings: config.Settings,
    *,
    container: deps.ServiceContainer | None = None,
    container_factory: Callable[[config.Settings], deps.ServiceContainer] | None = None,
) -> fastapi.FastAPI:
    """``container``: use this one (already wired). ``container_factory``: build one at startup."""
    if container is None and container_factory is None:
        raise ValueError("give a container or a container_factory")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @contextlib.asynccontextmanager
    async def lifespan(app: fastapi.FastAPI):
        c: deps.ServiceContainer = (
            app.state.container if getattr(app.state, "container", None) else container_factory(settings)
        )
        app.state.container = c
        await c.start()
        try:
            yield
        finally:
            await c.stop()

    app = fastapi.FastAPI(
        title="Autonomic Job Engine", version=aje.__version__, description=DESCRIPTION, lifespan=lifespan
    )
    if container is not None:
        app.state.container = container
    if settings.cors_origins:
        app.add_middleware(
            cors.CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"]
        )
    errors.install_error_handlers(app)

    secured = [fastapi.Depends(deps.require_api_key)]
    for module in (jobs, groups, schedules, contracts, internal):
        app.include_router(module.router, dependencies=secured)

    @app.get("/metrics", tags=["system"], include_in_schema=False)
    async def prometheus_metrics():
        return responses.Response(
            prometheus_client.generate_latest(), media_type=prometheus_client.CONTENT_TYPE_LATEST
        )

    @app.get("/healthz", tags=["system"])
    async def healthz():
        return {"status": "ok"}

    @app.get("/v1/info", tags=["system"], dependencies=secured)
    async def info(c: deps.ContainerDep):
        eng = c.engine
        return {
            "version": aje.__version__,
            "backend": eng.backend.name,
            "active_jobs": len(eng.managers),
            "groups": len(eng.groups),
            "schedules": len(eng.scheduler.schedules),
            "patterns": {s: dsl.CATALOGS[s].knobs for s in ("farm", "data_parallel")},
        }

    return app
