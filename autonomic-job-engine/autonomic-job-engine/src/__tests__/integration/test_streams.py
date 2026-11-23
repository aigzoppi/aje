"""Job and group streams: replay, resume, end, 204, task messages, slow clients, counters.

Findings 1-6, 8 and 9 of docs/job-status-mechanism.md, on the real engine, PostgreSQL and RabbitMQ.
"""

import functools

from aje.infrastructure.persistence import db
from common import api as api_lib
from common import specs
from common import sse

TERMINAL = {"job.succeeded", "job.failed", "job.cancelled"}


def kinds(messages: list[sse.Message]) -> list[str]:
    return [m.kind() for m in messages if m.event == "event"]


def saw(kind: str):
    return lambda messages: kind in kinds(messages)


async def finished_job(probe, **fields):
    """A farm job run to completion and no longer managed."""
    m = await probe.submit(specs.farm(tasks=[1, 2], close_when_done=True, management="passive", **fields))
    await probe.succeeded(m)
    await probe.wait(lambda: m.id not in probe.engine.managers)
    return m


class JobStreamHistoryTest:
    """Findings 1-3 over HTTP: ids, replay, Last-Event-ID resume, end with status, 204."""

    async def test_a_finished_job_replays_its_history_then_ends(self, probe, client):
        m = await finished_job(probe)
        r = await client.get(f"/v1/jobs/{m.id}/stream")
        assert r.status_code == 200
        messages = sse.parse(r.text)
        events = [x for x in messages if x.event == "event"]
        assert events[0].kind() == "job.submitted" and events[-1].kind() == "job.succeeded"
        assert all(x.id == str(x.data["id"]) for x in events)  # the SSE id is the event id
        assert sse.event_ids(events) == sorted(set(sse.event_ids(events)))
        assert messages[-1].event == "end" and messages[-1].data == {"job_id": m.id, "status": "succeeded"}

    async def test_a_reconnect_resumes_after_last_event_id(self, probe, client):
        m = await finished_job(probe)
        ids = sse.event_ids(sse.parse((await client.get(f"/v1/jobs/{m.id}/stream")).text))
        r = await client.get(f"/v1/jobs/{m.id}/stream", headers={"Last-Event-ID": str(ids[1])})
        assert sse.event_ids(sse.parse(r.text)) == ids[2:]

    async def test_after_id_selects_the_replay_start(self, probe, client):
        m = await finished_job(probe)
        ids = sse.event_ids(sse.parse((await client.get(f"/v1/jobs/{m.id}/stream")).text))
        r = await client.get(f"/v1/jobs/{m.id}/stream", params={"after_id": ids[-2]})
        assert sse.event_ids(sse.parse(r.text)) == ids[-1:]

    async def test_after_the_end_a_reconnect_gets_204(self, probe, client):
        m = await finished_job(probe)
        last = sse.event_ids(sse.parse((await client.get(f"/v1/jobs/{m.id}/stream")).text))[-1]
        r = await client.get(f"/v1/jobs/{m.id}/stream", headers={"Last-Event-ID": str(last)})
        assert r.status_code == 204 and not r.content

    async def test_an_unknown_job_is_404(self, client):
        assert (await client.get("/v1/jobs/nope/stream")).status_code == 404


class JobStreamLiveTest:
    async def test_connecting_late_still_delivers_earlier_events(self, probe, services):
        m = await probe.submit(specs.farm(management="passive"))
        stream = await services.streams.job_stream(m.id, sse.connected, metrics_every=60)
        first = await sse.read(stream, saw("job.started"))
        assert kinds(first)[:2] == ["job.submitted", "job.started"]

        await m.cancel("test")
        rest = await sse.read(stream, sse.ended)
        assert "job.cancelled" in kinds(rest)
        assert rest[-1].event == "end" and rest[-1].data == {"job_id": m.id, "status": "cancelled"}
        ids = sse.event_ids(first + rest)
        assert ids == sorted(set(ids))  # no event twice, none out of order

    async def test_task_messages_on_request(self, probe, services):
        m = await probe.submit(specs.farm(management="passive"))
        stream = await services.streams.job_stream(m.id, sse.connected, metrics_every=60, tasks=True)
        await m.submit_tasks([3, 4])
        messages = await sse.read(stream, lambda ms: sum(x.event == "task" for x in ms) >= 2)
        await stream.aclose()
        tasks = [x.data for x in messages if x.event == "task"]
        assert {t["kind"] for t in tasks} == {"task.done"}
        assert sorted(t["data"]["result"] for t in tasks) == [9, 16]
        assert all(x.id is None for x in messages if x.event == "task")  # live only, not replayable

    async def test_no_task_messages_by_default(self, probe, services):
        m = await probe.submit(specs.farm(management="passive"))
        stream = await services.streams.job_stream(m.id, sse.connected, metrics_every=0.5)
        await m.submit_tasks([3])
        await probe.wait(lambda: m.counts()["completed"] == 1)
        await m.cancel("test")
        messages = await sse.read(stream, sse.ended)
        assert not [x for x in messages if x.event == "task"]

    async def test_a_slow_client_gets_reset_and_resumes_without_gaps(self, probe, services, monkeypatch):
        m = await probe.submit(specs.farm(management="passive"))
        bus = probe.engine.bus
        monkeypatch.setattr(bus, "subscribe", functools.partial(bus.subscribe, maxsize=2))
        stream = await services.streams.job_stream(m.id, sse.connected, metrics_every=60)
        seen = await sse.read(stream, saw("job.started"))

        for i in range(5):  # the client does not read: its queue overflows
            await bus.emit("rule.log", f"line {i}", job_id=m.id)
        reset = await sse.read(stream, sse.ended)
        assert reset[-1].event == "reset"

        resumed = await services.streams.job_stream(
            m.id, sse.connected, metrics_every=60, after_id=sse.event_ids(seen + reset)[-1]
        )
        again = await sse.read(resumed, lambda ms: kinds(ms).count("rule.log") == 5)
        await resumed.aclose()
        assert [x.data["message"] for x in again if x.kind() == "rule.log"] == [f"line {i}" for i in range(5)]


class GroupStreamTest:
    """Finding 8: initial facts, end on cancel, 204 afterwards."""

    async def test_facts_then_end_then_204(self, probe, services, client):
        g = await probe.submit_group(specs.group(specs.farm("a", management="passive"), management="passive"))
        stream = await services.streams.group_stream(g.id, sse.connected, metrics_every=60)
        first = await sse.read(stream, lambda ms: any(x.event == "facts" for x in ms))
        assert "group.created" in kinds(first)
        assert first[-1].event == "facts" and first[-1].data["Group"][0]["id"] == g.id

        await g.for_members("cancel")
        rest = await sse.read(stream, sse.ended)
        assert {"job.cancelled", "group.cancelled"} <= set(kinds(rest))
        assert rest[-1].event == "end" and rest[-1].data == {"group_id": g.id, "status": "cancelled"}

        last = sse.event_ids(first + rest)[-1]
        r = await client.get(f"/v1/groups/{g.id}/stream", headers={"Last-Event-ID": str(last)})
        assert r.status_code == 204


class InternalStreamResumeTest:
    async def test_replays_only_when_asked(self, probe, services):
        m = await finished_job(probe)
        live_only = services.streams.internal_stream(sse.connected, job_id=m.id, snapshot_every=0.2)
        messages = await sse.read(live_only, lambda ms: sum(x.event == "snapshot" for x in ms) >= 2)
        await live_only.aclose()
        assert not kinds(messages)

        replayed = services.streams.internal_stream(sse.connected, job_id=m.id, after_id=0, snapshot_every=60)
        messages = await sse.read(replayed, saw("job.succeeded"))
        await replayed.aclose()
        assert kinds(messages)[0] == "job.submitted"


class CountsTest:
    """Finding 4: one counters shape for live and finished jobs, on every endpoint."""

    async def test_finished_farm_jobs_report_the_live_keys(self, probe, api):
        m = await finished_job(probe)
        expected = {"submitted": 2, "completed": 2, "failed": 0, "retried": 0, "outstanding": 0}
        assert api_lib.expect(await api.jobs.get(m.id))["counts"] == expected
        [listed] = [j for j in api_lib.expect(await api.jobs.list()) if j["id"] == m.id]
        assert listed["counts"] == expected

    async def test_finished_data_parallel_jobs_include_batches(self, probe, api):
        m = await probe.submit(specs.dp(gather="sum", partitions=2, items=[1, 2, 3, 4], close_when_done=True))
        await probe.succeeded(m)
        await probe.wait(lambda: m.id not in probe.engine.managers)
        counts = api_lib.expect(await api.jobs.get(m.id))["counts"]
        assert counts["completed"] == 2 and counts["batches_done"] == 1 and counts["batches_pending"] == 0

    async def test_jobs_finished_before_counts_were_stored_are_rebuilt(self, probe, api):
        await probe.engine.store.add(
            db.JobRow(
                id="legacy1",
                name="legacy",
                pattern="farm",
                status="succeeded",
                management="passive",
                workers=0,
                closed=True,
                spec=specs.farm("legacy").model_dump(mode="json"),
            ),
            db.TaskRow(id="t1", job_id="legacy1", status="done", result=1),
            db.TaskRow(id="t2", job_id="legacy1", status="done", attempts=2, result=4),
            db.TaskRow(id="t3", job_id="legacy1", status="failed", attempts=2, error="x"),
        )
        assert api_lib.expect(await api.jobs.get("legacy1"))["counts"] == {
            "submitted": 3,
            "completed": 2,
            "failed": 1,
            "retried": 2,
            "outstanding": 0,
        }
