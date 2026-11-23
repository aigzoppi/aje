"""/internal/events: snapshots, persisted events and live task results (the Fractal Lab feed)."""

import asyncio
from collections.abc import Callable
import json

from aje import container
from aje.application import schemas
from aje_worker.examples import fractal
from common import api
from common import specs

Message = tuple[str, dict]


async def _never_disconnects() -> bool:
    return False


class InternalStream:
    """One internal event stream read in-process; ``async with`` closes it."""

    def __init__(self, engine, snapshot_every: float = 0.2, **options):
        self.service = container.Container(engine).streams
        self.options = {"snapshot_every": snapshot_every, **options}
        self._gen = None

    async def __aenter__(self) -> "InternalStream":
        self._gen = self.service.internal_stream(_never_disconnects, **self.options)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._gen.aclose()

    async def first(self) -> Message:
        msg = await anext(self._gen)
        return msg["event"], json.loads(msg["data"])

    async def read_until(self, done: Callable[[list[Message]], bool], timeout: float = 20.0) -> list[Message]:
        out: list[Message] = []

        async def run():
            async for msg in self._gen:
                out.append((msg["event"], json.loads(msg["data"])))
                if done(out):
                    return

        await asyncio.wait_for(run(), timeout)
        return out


def a_job_succeeded(messages: list[Message]) -> bool:
    return any(ev == "event" and data["kind"] == "job.succeeded" for ev, data in messages)


def fractal_tasks(size: int = 32, tile: int = 16) -> list[dict]:
    return [
        {
            "kind": "mandelbrot",
            "view": [-2.0, -1.5, 1.0, 1.5],
            "image": [size, size],
            "tile": t,
            "max_iter": 64,
        }
        for t in fractal.split_image(size, size, tile)
    ]


def fractal_job(name: str):
    return specs.farm(
        name=name, handler="fractal", tasks=fractal_tasks(), close_when_done=True, management="passive"
    )


class InternalStreamTest:
    async def test_delivers_snapshots_events_and_tiles(self, engine, probe):
        async with InternalStream(engine, tasks=True) as stream:
            assert (await stream.first())[0] == "snapshot"
            m = await probe.submit(fractal_job("mandel"))
            messages = await stream.read_until(a_job_succeeded)
            snapshot = stream.service.snapshot(job_id=m.id)

        tiles = [d for ev, d in messages if ev == "task" and d["job_id"] == m.id and d["kind"] == "task.done"]
        expected = fractal_tasks()
        assert sorted(tuple(t["data"]["result"]["tile"]) for t in tiles) == sorted(
            tuple(t["tile"]) for t in expected
        )
        assert all(t["data"]["worker"] and t["data"]["result"]["data"] for t in tiles)
        assert tiles[-1]["data"]["counts"]["completed"] == len(expected)
        assert snapshot[0]["status"] == "succeeded"  # finished jobs stay visible by id
        assert m.status == schemas.JobStatus.SUCCEEDED

    async def test_task_results_only_when_requested(self, engine, probe):
        async with InternalStream(engine, tasks=False) as stream:
            await stream.first()
            await probe.submit(fractal_job("quiet"))
            messages = await stream.read_until(a_job_succeeded)
        assert not any(ev == "task" for ev, _ in messages)
        assert not engine.bus.wants_notifications()  # the subscription is gone

    async def test_filters_by_job(self, engine, probe):
        mine = await probe.submit(specs.farm(name="a", management="passive"))
        other = await probe.submit(specs.farm(name="b", management="passive"))
        async with InternalStream(engine, job_id=mine.id, tasks=True) as stream:
            _, snapshot = await stream.first()
            assert [j["id"] for j in snapshot["jobs"]] == [mine.id]
            await mine.submit_tasks([1, 2])
            await other.submit_tasks([3, 4])
            messages = await stream.read_until(lambda ms: sum(ev == "task" for ev, _ in ms) >= 2)
        assert {d["job_id"] for ev, d in messages if ev in ("task", "event")} == {mine.id}
        await probe.wait(lambda: other.counts()["completed"] == 2)

    async def test_endpoint_requires_the_api_key(self, engine, client):
        engine.settings.api_key = "s3cret"
        api.expect(await client.get("/internal/events"), 401)
        api.expect(await client.get("/internal/events", params={"api_key": "wrong"}), 401)
