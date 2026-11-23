"""Composition root of the HTTP application: ``uvicorn aje.main:app`` or ``python -m aje``.

This is the only place that joins the HTTP adapter (``aje.interfaces.http``) with the
concrete wiring (``aje.container``).
"""

from __future__ import annotations

import fastapi

from aje import config
from aje import container as container_lib
from aje.application.runtime import engine as engine_lib
from aje.interfaces.http import app as app_lib


def create_app(
    engine: engine_lib.AutonomicEngine | None = None,
    settings: config.Settings | None = None,
    container: container_lib.Container | None = None,
) -> fastapi.FastAPI:
    """Builds the HTTP app.

    Args:
        engine: An engine to wrap (tests).
        settings: Settings; defaults to the container's or the environment's.
        container: A container to wrap (tests).

    Returns:
        The app. Without an engine or container, one is built from settings at startup.
    """
    if container is None and engine is not None:
        container = container_lib.Container(engine)
    settings = settings or (container.settings if container else config.get_settings())
    return app_lib.create_app(settings, container=container, container_factory=container_lib.build_container)


app = create_app()
