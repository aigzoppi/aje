"""HTTP API against the real engine, driven through ApiClient."""

import pytest

from aje import main
from common import api as api_lib
from common import polling
from common import specs

SQUARES = specs.farm_body("sq", max_workers=4)


class JobsTest:
    async def test_submit_monitor_and_finish_farm(self, api, engine):
        job = api_lib.expect(await api.jobs.create({**SQUARES, "tasks": [1, 2, 3]}), 201)
        assert job["workers"] == 1 and job["status"] == "running"
        jid = job["id"]
        assert len(api_lib.expect(await api.jobs.add_tasks(jid, [4, 5], close=True), 202)["task_ids"]) == 2

        await polling.wait_for(lambda: engine.job(jid).status.value == "succeeded")
        await polling.wait_for(lambda: jid not in engine.managers)  # finished: counts come from the job row
        finished = api_lib.expect(await api.jobs.get(jid))
        assert finished["status"] == "succeeded" and finished["counts"] == {
            "submitted": 5,
            "completed": 5,
            "failed": 0,
            "retried": 0,
            "outstanding": 0,
        }
        tasks = api_lib.expect(await api.jobs.tasks(jid, include_payload=True))
        assert sorted(t["result"] for t in tasks) == [1, 4, 9, 16, 25]
        kinds = [e["kind"] for e in api_lib.expect(await api.jobs.events(jid))]
        assert kinds[0] == "job.submitted" and kinds[-1] == "job.succeeded"
        api_lib.expect(await api.jobs.add_tasks(jid, [1]), 409)

    async def test_control_interface(self, api):
        jid = api_lib.expect(await api.jobs.create({**SQUARES, "management": "passive"}), 201)["id"]
        assert api_lib.expect(await api.jobs.scale(jid, 3))["workers"] == 3
        assert api_lib.expect(await api.jobs.suspend(jid))["status"] == "suspended"
        api_lib.expect(await api.jobs.suspend(jid), 409)
        assert api_lib.expect(await api.jobs.resume(jid))["status"] == "running"
        assert "history" in api_lib.expect(await api.jobs.metrics(jid))
        assert api_lib.expect(await api.jobs.facts(jid))["Job"][0]["workers"] == 3
        contract = api_lib.expect(
            await api.jobs.set_contract(jid, {"builtin": "farm-throughput", "globals": {"idle_after": 5}})
        )
        assert contract["parameters"]["idle_after"] == 5
        assert api_lib.expect(await api.jobs.delete(jid))["status"] == "cancelled"
        api_lib.expect(await api.jobs.delete(jid), 409)

    async def test_data_parallel_batches(self, api):
        body = specs.dp_body("dp", gather="sum", partitions=2)
        jid = api_lib.expect(await api.jobs.create(body), 201)["id"]
        batch = api_lib.expect(await api.jobs.add_batch(jid, list(range(10)), partitions=5), 202)
        assert batch["partitions"] == 5

        async def gathered():
            b = api_lib.expect(await api.jobs.batch(jid, batch["batch_id"]))
            return b if b["status"] == "done" else None

        assert (await polling.wait_for(gathered))["result"] == 285
        assert api_lib.expect(await api.jobs.set_partitions(jid, 7))["partitions"] == 7
        api_lib.expect(await api.jobs.add_tasks(jid, [1]), 400)  # a farm-only operation

    async def test_unknown_job_is_not_found(self, api):
        api_lib.expect(await api.jobs.get("nope"), 404)


class ValidationTest:
    @pytest.mark.parametrize(
        "change, message",
        [
            ({"contract": {"source": 'rule "x" when Job() then set_partitions(2); end'}}, "not allowed"),
            ({"workers": 9}, None),  # above max_workers
            ({"name": "Bad_Name"}, None),
        ],
    )
    async def test_invalid_jobs_are_rejected(self, api, change, message):
        error = api_lib.expect(await api.jobs.create({**SQUARES, **change}), 422)
        if message:
            assert message in error["error"]["message"]

    async def test_contract_validation_checks_the_scope(self, api):
        assert (
            api_lib.expect(await api.contracts.validate("data_parallel", {"builtin": "dp-balance"}))["valid"]
            is True
        )
        api_lib.expect(await api.contracts.validate("farm", {"builtin": "dp-balance"}), 422)

    async def test_catalog_lists_the_actions_of_a_scope(self, api):
        actions = api_lib.expect(await api.contracts.catalog("farm"))["actions"]
        assert "scale" in actions and "set_partitions" not in actions


class GroupsTest:
    async def test_budget_members_and_cancel(self, api):
        members = [
            specs.farm_body("a", max_workers=4, workers=2),
            specs.farm_body("b", max_workers=4, workers=2),
        ]
        g = api_lib.expect(
            await api.groups.create(
                {"name": "grp", "max_workers": 3, "management": "passive", "jobs": members}
            ),
            201,
        )
        assert g["used_workers"] == 3 and len(g["jobs"]) == 2
        gid = g["id"]
        assert api_lib.expect(await api.groups.add_job(gid, {**SQUARES, "name": "c"}), 201)["group_id"] == gid
        assert "Escalation" in api_lib.expect(await api.groups.facts(gid))
        assert api_lib.expect(await api.groups.set_budget(gid, 2))["max_workers"] == 2
        assert set(api_lib.expect(await api.groups.cancel(gid))["results"].values()) == {"ok"}


class OperationsTest:
    async def test_api_key_protects_the_api_but_not_health(self, engine):
        engine.settings.api_key = "s3cret"
        async with api_lib.asgi_client(main.create_app(engine)) as http:
            api = api_lib.ApiClient(http)
            api_lib.expect(await api.jobs.list(), 401)
            api_lib.expect(await http.get("/v1/jobs", headers={"X-API-Key": "s3cret"}))
            api_lib.expect(await http.get("/healthz"))

    async def test_prometheus_endpoint(self, api):
        api_lib.expect(await api.jobs.create({**SQUARES, "tasks": [1]}), 201)
        r = await api.http.get("/metrics")
        assert r.status_code == 200 and "aje_job_workers" in r.text
