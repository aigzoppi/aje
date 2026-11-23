"""FastAPI dependency providers.

Routers declare what they need (``JobServiceDep``...) and never build anything
themselves; tests can swap any provider with ``app.dependency_overrides``.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Protocol

import fastapi
from fastapi import status

from aje import config
from aje.application import services
from aje.application.runtime import engine as engine_lib


class ServiceContainer(Protocol):
    """What the HTTP adapter needs from the composition root.

    ``aje.container.Container`` provides it; the adapter never builds it, so it does not
    depend on the concrete wiring.
    """

    settings: config.Settings
    engine: engine_lib.AutonomicEngine
    jobs: services.JobService
    groups: services.GroupService
    schedules: services.ScheduleService
    contracts: services.ContractService
    streams: services.StreamService

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


def get_container(request: fastapi.Request) -> ServiceContainer:
    return request.app.state.container


ContainerDep = Annotated[ServiceContainer, fastapi.Depends(get_container)]


def get_job_service(c: ContainerDep) -> services.JobService:
    return c.jobs


def get_group_service(c: ContainerDep) -> services.GroupService:
    return c.groups


def get_schedule_service(c: ContainerDep) -> services.ScheduleService:
    return c.schedules


def get_contract_service(c: ContainerDep) -> services.ContractService:
    return c.contracts


def get_stream_service(c: ContainerDep) -> services.StreamService:
    return c.streams


JobServiceDep = Annotated[services.JobService, fastapi.Depends(get_job_service)]
GroupServiceDep = Annotated[services.GroupService, fastapi.Depends(get_group_service)]
ScheduleServiceDep = Annotated[services.ScheduleService, fastapi.Depends(get_schedule_service)]
ContractServiceDep = Annotated[services.ContractService, fastapi.Depends(get_contract_service)]
StreamServiceDep = Annotated[services.StreamService, fastapi.Depends(get_stream_service)]


async def require_api_key(
    c: ContainerDep,
    x_api_key: str | None = fastapi.Header(None),
    api_key: str | None = fastapi.Query(None, include_in_schema=False),
) -> None:
    """``X-API-Key`` header, or ``?api_key=`` for browser EventSource clients that cannot set headers."""
    expected = c.settings.api_key
    given = x_api_key or api_key
    if expected and not (given and secrets.compare_digest(given, expected)):
        raise fastapi.HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid X-API-Key")
