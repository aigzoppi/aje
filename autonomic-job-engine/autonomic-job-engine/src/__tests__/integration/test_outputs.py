"""Job outputs on RabbitMQ: msgpack messages on an output stream independent of the job."""

import contextlib
import re

import pytest_asyncio

from aje_worker import protocol
from common import api as api_lib
from common import output as output_lib
from common import specs


@pytest_asyncio.fixture(name="outputs")
async def outputs_fixture(amqp_url):
    """Factory: open an OutputReader on an output id; every reader is closed at teardown."""
    async with contextlib.AsyncExitStack() as stack:

        async def open_stream(output_id: str) -> output_lib.OutputReader:
            return await stack.enter_async_context(output_lib.OutputReader(amqp_url, output_id))

        yield open_stream


def kinds(messages: list[output_lib.Output], kind: str) -> list[output_lib.Output]:
    return [m for m in messages if m.body["kind"] == kind]


class FarmOutputTest:
    async def test_results_are_published_with_their_job_id(self, probe, outputs):
        stream = await outputs("squares")
        m = await probe.submit(
            specs.farm(tasks=[1, 2, 3], close_when_done=True, management="passive", output_queue="squares")
        )
        messages = await stream.read_until(output_lib.finished(m.id))

        assert sorted(o.body["result"] for o in kinds(messages, "task.result")) == [1, 4, 9]
        assert all(o.body["job_id"] == m.id and o.headers["job_id"] == m.id for o in messages)
        assert all(o.content_type == protocol.CONTENT_TYPE and o.type == o.body["kind"] for o in messages)
        assert len({o.message_id for o in messages}) == len(messages)  # idempotency keys
        last = messages[-1].body
        assert last["kind"] == "job.finished" and last["status"] == "succeeded"
        assert last["counts"]["completed"] == 3

    async def test_permanent_failures_are_published(self, probe, outputs):
        stream = await outputs("failures")
        m = await probe.submit(
            specs.farm(
                handler="always_fail", tasks=[7], close_when_done=True, max_retries=1, output_queue="failures"
            )
        )
        messages = await stream.read_until(output_lib.finished(m.id))
        [failed] = kinds(messages, "task.failed")
        assert failed.body["attempt"] == 2 and "ValueError" in failed.body["error"]
        assert not kinds(messages, "task.result")

    async def test_jobs_can_share_a_stream(self, probe, outputs):
        stream = await outputs("shared")
        jobs = [
            await probe.submit(
                specs.farm(name, tasks=[2], close_when_done=True, management="passive", output_queue="shared")
            )
            for name in ("a", "b")
        ]
        messages = await stream.read_until(output_lib.finished(*(j.id for j in jobs)))
        assert {o.body["job_id"] for o in kinds(messages, "task.result")} == {j.id for j in jobs}


class DataParallelOutputTest:
    async def test_only_gathered_batches_are_published(self, probe, outputs):
        stream = await outputs("sums")
        m = await probe.submit(
            specs.dp(
                gather="sum", partitions=3, items=list(range(10)), close_when_done=True, output_queue="sums"
            )
        )
        messages = await stream.read_until(output_lib.finished(m.id))
        [batch] = kinds(messages, "batch.result")
        assert batch.body["result"] == sum(i * i for i in range(10)) and batch.body["partitions"] == 3
        batch_id = batch.body["batch_id"]
        assert batch.message_id == f"{m.id}:{batch_id}"
        assert not kinds(messages, "task.result")  # partitions are internal


class OutputIdTest:
    async def test_a_unique_id_is_generated_when_omitted(self, api, outputs):
        job = api_lib.expect(
            await api.jobs.create(specs.farm_body("auto", tasks=[3], close_when_done=True)), 201
        )
        output_id = job["output_queue"]
        assert re.fullmatch(r"[0-9a-f]{32}", output_id) and output_id != job["id"]
        messages = await (await outputs(output_id)).read_until(output_lib.finished(job["id"]))
        assert [o.body["result"] for o in kinds(messages, "task.result")] == [9]

    async def test_invalid_ids_are_rejected(self, api):
        api_lib.expect(await api.jobs.create(specs.farm_body("bad", output_queue="no spaces")), 422)
