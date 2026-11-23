"""Cron schedules through the API: validation, concurrency policies, timing, lifecycle."""

import datetime

import pytest

from aje.application.runtime import scheduler as scheduler_lib
from aje.infrastructure.persistence import db
from common import api as api_lib
from common import polling
from common import specs

FINITE = specs.template(tasks=[1, 2, 3], close_when_done=True)
ENDLESS = specs.template(management="passive")
NIGHTLY = {"name": "nightly", "cron": "0 2 * * *", "job": FINITE}


@pytest.fixture(name="new_schedule")
def new_schedule_fixture(api):
    """Factory: create a schedule (``NIGHTLY`` plus overrides) and return its JSON."""

    async def create(**fields):
        return api_lib.expect(await api.schedules.create({**NIGHTLY, **fields}), 201)

    return create


@pytest.fixture(name="scheduler")
def scheduler_fixture(engine):
    return engine.scheduler


class CreationTest:
    async def test_create_read_and_list(self, api, new_schedule):
        s = await new_schedule(timezone="Europe/Rome")
        assert s["status"] == "active" and s["next_run_at"] is not None and s["active_jobs"] == []
        assert api_lib.expect(await api.schedules.get(s["id"]))["cron"] == "0 2 * * *"
        assert [x["id"] for x in api_lib.expect(await api.schedules.list())] == [s["id"]]

    @pytest.mark.parametrize(
        "change, status",
        [
            ({"cron": "61 * * * *"}, 422),
            ({"timezone": "Mars/Base"}, 422),
            ({"name": "a" * 32}, 422),
            ({"job": {**FINITE, "contract": {"builtin": "nope"}}}, 422),
            ({"group_id": "nope"}, 400),
        ],
    )
    async def test_invalid_schedules_are_rejected(self, api, change, status):
        api_lib.expect(await api.schedules.create({**NIGHTLY, "name": "x", **change}), status)

    async def test_names_are_unique(self, api, new_schedule):
        await new_schedule()
        api_lib.expect(await api.schedules.create(NIGHTLY), 400)


class RunsTest:
    async def test_trigger_runs_a_labelled_job(self, api, engine, new_schedule):
        s = await new_schedule()
        run = api_lib.expect(await api.schedules.trigger(s["id"]), 201)
        assert run["status"] == "started" and run["trigger"] == "manual"
        jid = run["job_id"]
        job = engine.job(jid)
        assert job.name.startswith("nightly-") and job.spec.labels[scheduler_lib.LABEL_SCHEDULE] == s["id"]

        await polling.wait_for(lambda: engine.job(jid).status.value == "succeeded")
        assert api_lib.expect(await api.schedules.runs(s["id"]))[0]["job_status"] == "succeeded"
        assert api_lib.expect(await api.schedules.get(s["id"]))["last_job_id"] == jid


class ConcurrencyPoliciesTest:
    async def test_forbid_skips_while_previous_run_is_active(self, api, new_schedule):
        sid = (await new_schedule(job=ENDLESS, concurrency_policy="forbid"))["id"]
        first = api_lib.expect(await api.schedules.trigger(sid), 201)["job_id"]
        assert first in api_lib.expect(await api.schedules.trigger(sid), 409)["detail"]
        assert [r["status"] for r in api_lib.expect(await api.schedules.runs(sid))] == ["skipped", "started"]
        assert api_lib.expect(await api.schedules.get(sid))["active_jobs"] == [first]

    async def test_replace_cancels_previous_run(self, api, engine, new_schedule):
        sid = (await new_schedule(job=ENDLESS, concurrency_policy="replace"))["id"]
        first = api_lib.expect(await api.schedules.trigger(sid), 201)["job_id"]
        second = api_lib.expect(await api.schedules.trigger(sid), 201)["job_id"]
        assert engine.job(first).status.value == "cancelled"
        assert api_lib.expect(await api.schedules.get(sid))["active_jobs"] == [second]

    async def test_allow_runs_concurrently(self, api, new_schedule):
        sid = (await new_schedule(job=ENDLESS, concurrency_policy="allow"))["id"]
        for _ in range(2):
            api_lib.expect(await api.schedules.trigger(sid), 201)
        assert len(api_lib.expect(await api.schedules.get(sid))["active_jobs"]) == 2


class TimingTest:
    """Fire times; drives the scheduler loop directly instead of waiting for real cron times."""

    # pylint: disable=protected-access

    async def test_cron_loop_fires_when_due(self, api, scheduler, new_schedule):
        s = await new_schedule()
        row = scheduler.schedules[s["id"]]
        row.next_run_at = db.utcnow() - datetime.timedelta(seconds=1)
        scheduler._wake.set()
        await polling.wait_for(lambda: row.last_job_id is not None)
        run = api_lib.expect(await api.schedules.runs(s["id"]))[0]
        assert run["trigger"] == "cron" and run["status"] == "started"
        assert row.next_run_at > db.utcnow()

    @pytest.mark.parametrize("deadline, outcome", [(60, "skipped"), (None, "started")])
    async def test_catch_up_respects_starting_deadline(self, api, scheduler, new_schedule, deadline, outcome):
        sid = (await new_schedule(starting_deadline_seconds=deadline))["id"]
        async with scheduler._lock:  # as at engine start: the cron loop must not fire the same overdue time
            row = scheduler.schedules[sid]
            row.next_run_at = db.utcnow() - datetime.timedelta(seconds=120)
            await scheduler._catch_up(row, db.utcnow())
        run = api_lib.expect(await api.schedules.runs(sid))[0]
        assert run["status"] == outcome and run["trigger"] == "catch-up"


class LifecycleTest:
    async def test_suspend_and_resume(self, api, new_schedule):
        sid = (await new_schedule(job=ENDLESS))["id"]
        suspended = api_lib.expect(await api.schedules.suspend(sid))
        assert suspended["status"] == "suspended" and suspended["next_run_at"] is None
        api_lib.expect(await api.schedules.suspend(sid), 409)
        assert api_lib.expect(await api.schedules.resume(sid))["status"] == "active"

    async def test_update(self, api, new_schedule):
        sid = (await new_schedule(job=ENDLESS))["id"]
        updated = api_lib.expect(
            await api.schedules.update(sid, {"cron": "*/5 * * * *", "concurrency_policy": "allow"})
        )
        assert updated["cron"] == "*/5 * * * *"
        assert datetime.datetime.fromisoformat(updated["next_run_at"]).minute % 5 == 0
        api_lib.expect(await api.schedules.update(sid, {"cron": None}), 400)
        api_lib.expect(await api.schedules.update(sid, {"cron": "bad"}), 422)

    async def test_delete_cancels_active_runs_on_request(self, api, engine, new_schedule):
        sid = (await new_schedule(job=ENDLESS))["id"]
        jid = api_lib.expect(await api.schedules.trigger(sid), 201)["job_id"]
        assert api_lib.expect(await api.schedules.delete(sid, cancel_active=True))["cancelled_jobs"] == [jid]
        assert engine.job(jid).status.value == "cancelled"
        api_lib.expect(await api.schedules.get(sid), 404)
