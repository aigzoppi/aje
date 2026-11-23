"""ApiClient: the HTTP API as objects, one per resource.

Tests call ``api.jobs.scale(jid, 3)`` instead of building URLs, and check responses with
``expect(response, status)``, which also returns the JSON body. New endpoints extend a
resource class; tests that use the others are untouched.
"""

from collections.abc import AsyncIterator
import contextlib
from typing import Any, ClassVar

import fastapi
import httpx


@contextlib.asynccontextmanager
async def asgi_client(app: fastapi.FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client talking to ``app`` in-process."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        yield client


def expect(response: httpx.Response, status: int = 200) -> Any:
    """Assert the status code and return the decoded body (None when empty)."""
    request = response.request
    assert response.status_code == status, (
        f"{request.method} {request.url.path} -> {response.status_code}, expected {status}: {response.text}"
    )
    return response.json() if response.content else None


class Resource:
    """A REST collection: ``path`` lists and creates, ``path/<id>`` reads and deletes."""

    path: ClassVar[str]

    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    def url(self, *parts: Any) -> str:
        return "/".join([self.path, *map(str, parts)])

    async def create(self, body: dict) -> httpx.Response:
        return await self.http.post(self.path, json=body)

    async def list(self, **params: Any) -> httpx.Response:
        return await self.http.get(self.path, params=params)

    async def get(self, rid: str) -> httpx.Response:
        return await self.http.get(self.url(rid))

    async def delete(self, rid: str, **params: Any) -> httpx.Response:
        return await self.http.delete(self.url(rid), params=params)

    async def _post(self, rid: str, action: str, body: dict | None = None) -> httpx.Response:
        return await self.http.post(self.url(rid, action), json=body)

    async def _put(self, rid: str, action: str, body: dict) -> httpx.Response:
        return await self.http.put(self.url(rid, action), json=body)

    async def _get(self, rid: str, *parts: Any, **params: Any) -> httpx.Response:
        return await self.http.get(self.url(rid, *parts), params=params)


class JobsApi(Resource):
    """/v1/jobs: submit and control jobs, feed them tasks or batches."""

    path = "/v1/jobs"

    async def add_tasks(self, jid: str, tasks: list, close: bool = False) -> httpx.Response:
        return await self._post(jid, "tasks", {"tasks": tasks, "close": close})

    async def tasks(self, jid: str, **params: Any) -> httpx.Response:
        return await self._get(jid, "tasks", **params)

    async def add_batch(self, jid: str, items: list, partitions: int | None = None) -> httpx.Response:
        body: dict = {"items": items}
        if partitions is not None:
            body["partitions"] = partitions
        return await self._post(jid, "batches", body)

    async def batch(self, jid: str, bid: str) -> httpx.Response:
        return await self._get(jid, "batches", bid)

    async def scale(self, jid: str, workers: int) -> httpx.Response:
        return await self._post(jid, "scale", {"workers": workers})

    async def set_partitions(self, jid: str, partitions: int) -> httpx.Response:
        return await self._post(jid, "partitions", {"partitions": partitions})

    async def suspend(self, jid: str) -> httpx.Response:
        return await self._post(jid, "suspend")

    async def resume(self, jid: str) -> httpx.Response:
        return await self._post(jid, "resume")

    async def set_contract(self, jid: str, contract: dict) -> httpx.Response:
        return await self._put(jid, "contract", contract)

    async def events(self, jid: str) -> httpx.Response:
        return await self._get(jid, "events")

    async def metrics(self, jid: str) -> httpx.Response:
        return await self._get(jid, "metrics")

    async def facts(self, jid: str) -> httpx.Response:
        return await self._get(jid, "facts")


class GroupsApi(Resource):
    """/v1/groups: groups of jobs sharing a worker budget."""

    path = "/v1/groups"

    async def add_job(self, gid: str, body: dict) -> httpx.Response:
        return await self._post(gid, "jobs", body)

    async def facts(self, gid: str) -> httpx.Response:
        return await self._get(gid, "facts")

    async def set_budget(self, gid: str, max_workers: int) -> httpx.Response:
        return await self._put(gid, "budget", {"max_workers": max_workers})

    async def cancel(self, gid: str) -> httpx.Response:
        return await self._post(gid, "cancel")


class SchedulesApi(Resource):
    """/v1/schedules: cron schedules and their runs."""

    path = "/v1/schedules"

    async def update(self, sid: str, patch: dict) -> httpx.Response:
        return await self.http.patch(self.url(sid), json=patch)

    async def trigger(self, sid: str) -> httpx.Response:
        return await self._post(sid, "trigger")

    async def runs(self, sid: str) -> httpx.Response:
        return await self._get(sid, "runs")

    async def suspend(self, sid: str) -> httpx.Response:
        return await self._post(sid, "suspend")

    async def resume(self, sid: str) -> httpx.Response:
        return await self._post(sid, "resume")


class ContractsApi(Resource):
    """/v1/contracts: validate contracts and read action catalogs."""

    path = "/v1/contracts"

    async def validate(self, scope: str, contract: dict) -> httpx.Response:
        return await self.http.post(self.url("validate"), json={"scope": scope, "contract": contract})

    async def catalog(self, scope: str) -> httpx.Response:
        return await self.http.get(self.url("catalog", scope))


class ApiClient:
    """The whole API: one attribute per resource, plus the raw ``http`` client."""

    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self.jobs = JobsApi(http)
        self.groups = GroupsApi(http)
        self.schedules = SchedulesApi(http)
        self.contracts = ContractsApi(http)
