"""Application services on the real engine, repositories and PostgreSQL."""

import pytest

from aje.application import schemas
from aje.domain import errors
from common import specs


class JobServiceTest:
    def test_unknown_job_is_not_found(self, services):
        with pytest.raises(errors.NotFoundError):
            services.jobs.live("nope")

    async def test_finished_job_is_a_conflict(self, services, probe):
        m = await probe.succeeded(await probe.submit(specs.farm(tasks=[1], close_when_done=True)))
        with pytest.raises(errors.ConflictError, match="succeeded"):
            services.jobs.live(m.id)

    async def test_live_job_is_its_manager(self, services, probe):
        m = await probe.submit(specs.farm(management="passive"))
        assert services.jobs.live(m.id) is m

    async def test_batches_are_rejected_for_farm_jobs(self, services, probe):
        m = await probe.submit(specs.farm(management="passive"))
        with pytest.raises(errors.InvalidOperationError):
            await services.jobs.add_batch(m.id, [1], None)

    async def test_scale_returns_the_stored_view(self, services, probe):
        m = await probe.submit(specs.farm(management="passive", max_workers=4))
        view = await services.jobs.scale(m.id, 3)
        assert view.workers == 3 and view.status == schemas.JobStatus.RUNNING

    async def test_unknown_job_listing_is_not_found(self, services):
        with pytest.raises(errors.NotFoundError):
            await services.jobs.list_tasks("nope")


class StreamServiceTest:
    @pytest.fixture(name="jobs")
    async def jobs_fixture(self, probe) -> dict[str, str]:
        """A standalone job "a" and a job "b" in group "g": name -> id."""
        a = await probe.submit(specs.farm("a", management="passive"))
        g = await probe.submit_group(specs.group(specs.farm("b", management="passive"), management="passive"))
        [b] = g.members()
        return {"a": a.id, "b": b.id, "g": g.id}

    @pytest.mark.parametrize(
        "filters, expected",
        [({}, ["a", "b"]), ({"group_id": "g"}, ["b"]), ({"job_id": "a"}, ["a"]), ({"job_id": "zzz"}, [])],
    )
    def test_snapshot_filters_live_jobs(self, services, jobs, filters, expected):
        filters = {k: jobs.get(v, v) for k, v in filters.items()}
        ids = [j["id"] for j in services.streams.snapshot(**filters)]
        assert sorted(ids) == sorted(jobs[name] for name in expected)
