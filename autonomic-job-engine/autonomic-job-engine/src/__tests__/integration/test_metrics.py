"""Finding 10: the metrics the engine really produces validate against the typed schema."""

from aje.application import schemas
from common import api as api_lib
from common import specs
from common import sse


async def managed(probe, spec):
    """A submitted job, once its first management tick has produced metrics."""
    m = await probe.submit(spec)
    await probe.wait(lambda: m.last_metrics is not None and m.history)
    return m


class MetricsEndpointTest:
    async def test_farm_metrics(self, probe, api):
        m = await managed(probe, specs.farm(management="passive", tasks=[1, 2]))
        body = api_lib.expect(await api.jobs.metrics(m.id))
        out = schemas.JobMetricsOut.model_validate(body)
        assert out.job_id == m.id and out.current is not None and out.history
        assert out.current.partitions is None  # farm: no batch metrics
        assert set(body["current"]) == set(schemas.JobMetrics.model_fields)

    async def test_data_parallel_metrics(self, probe, api):
        m = await managed(probe, specs.dp(management="passive", partitions=3, items=[1, 2, 3]))
        current = schemas.JobMetricsOut.model_validate(api_lib.expect(await api.jobs.metrics(m.id))).current
        assert current.partitions == 3 and current.batches_failed == 0

    async def test_the_job_view_carries_the_same_metrics(self, probe, api):
        m = await managed(probe, specs.farm(management="passive"))
        job = api_lib.expect(await api.jobs.get(m.id))
        assert schemas.JobMetrics.model_validate(job["metrics"]).ts == m.last_metrics["ts"]


class StreamMetricsTest:
    async def test_sse_metrics_and_snapshots_use_the_schema(self, probe, services):
        m = await managed(probe, specs.farm(management="passive"))
        stream = await services.streams.job_stream(m.id, sse.connected, metrics_every=0.5)
        messages = await sse.read(stream, lambda ms: any(x.event == "metrics" and "ts" in x.data for x in ms))
        await stream.aclose()
        data = next(x.data for x in messages if x.event == "metrics" and "ts" in x.data)
        status, workers = data.pop("status"), data.pop("workers")
        assert status == "running" and workers == m.workers
        schemas.JobMetrics.model_validate(data)

        [snapshot] = services.streams.snapshot(job_id=m.id)
        schemas.JobMetrics.model_validate(snapshot["metrics"])
