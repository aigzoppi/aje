"""Backend port: how the runtime starts, scales and stops the workers of a job."""

from __future__ import annotations

from typing import Any, Protocol

import pydantic


class WorkerSpec(pydantic.BaseModel):
    """Everything a backend needs to run the workers of one job."""

    job_id: str
    job_name: str
    pattern: str
    image: str
    command: list[str]
    args: list[str] = pydantic.Field(default_factory=list)
    env: dict[str, str] = pydantic.Field(default_factory=dict)
    resources: dict[str, str | None] = pydantic.Field(default_factory=dict)
    node_selector: dict[str, str] = pydantic.Field(default_factory=dict)
    service_account: str | None = None
    image_pull_policy: str | None = None
    backoff_limit: int = 20
    labels: dict[str, str] = pydantic.Field(default_factory=dict)
    job_context: dict[str, Any] = pydantic.Field(
        default_factory=dict
    )  # jobcontext, delivered to every worker


class BackendStatus(pydantic.BaseModel):
    phase: str = "unknown"  # running | suspended | complete | failed | missing | unknown
    active: int = 0
    ready: int = 0
    succeeded: int = 0
    failed: int = 0
    message: str | None = None


class Backend(Protocol):
    """Runs workers for jobs (local processes, Kubernetes Jobs, in-process tasks)."""

    name: str

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def create(self, spec: WorkerSpec, workers: int) -> None: ...
    async def scale(self, job_id: str, workers: int) -> None: ...
    async def suspend(self, job_id: str) -> None: ...
    async def resume(self, job_id: str, workers: int) -> None: ...
    async def delete(self, job_id: str) -> None: ...
    async def status(self, job_id: str) -> BackendStatus: ...
    async def workers(self, job_id: str) -> list[dict[str, Any]]: ...
    async def logs(self, job_id: str, worker: str | None, tail: int) -> dict[str, str]: ...
