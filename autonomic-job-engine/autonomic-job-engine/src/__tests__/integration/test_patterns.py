"""Behaviour of the managers: real PostgreSQL and RabbitMQ, in-process workers speaking AMQP."""

import asyncio

import hypothesis
from hypothesis import strategies as st
import pytest

from aje.application import schemas
from aje.application.runtime import compiler
from common import engine as test_engine
from common import probe as probe_lib
from common import specs

SQUARES = [i * i for i in range(10)]


class FarmTest:
    async def test_defaults_to_one_worker_and_completes(self, probe):
        m = await probe.submit(specs.farm(tasks=list(range(10)), close_when_done=True, management="passive"))
        assert m.workers == 1
        await probe.succeeded(m)
        assert await probe.results(m) == SQUARES

    async def test_scales_up_on_backlog_and_down_when_idle(self, probe):
        contract = {"builtin": "farm-throughput", "globals": {"backlog_per_worker": 2, "idle_after": 1}}
        m = await probe.submit(
            specs.farm(handler="slow_square", max_workers=8, contract=contract, tasks=list(range(80)))
        )
        await probe.wait(lambda: m.workers >= 4, timeout=12)  # at most one "grow fast" cooldown (10 s)
        await probe.wait(lambda: m.counts()["completed"] == 80, timeout=20)
        await probe.wait(lambda: m.workers == 1, timeout=10)
        await asyncio.sleep(0.3)  # the event is persisted right after the knob changes
        messages = [e.message for e in await probe.events(m, "job.scaled")]
        assert any("grow fast on backlog" in msg for msg in messages)
        assert any("shrink" in msg for msg in messages)

    async def test_passive_job_never_scales_itself(self, probe):
        m = await probe.submit(specs.farm(handler="slow_square", management="passive", tasks=list(range(30))))
        await asyncio.sleep(1.0)
        assert m.workers == 1 and not await probe.events(m, "job.scaled")
        await m.set_workers(3, "api")
        assert m.workers == 3
        await m.set_management("active")
        await probe.wait(lambda: m.workers > 3, timeout=5)

    async def test_scale_is_bounded(self, probe):
        m = await probe.submit(specs.farm(management="passive", min_workers=1, max_workers=3))
        assert await m.set_workers(10, "api") == 3
        assert await m.set_workers(0, "api") == 1

    async def test_suspend_keeps_work_and_resume_finishes_it(self, probe):
        m = await probe.submit(specs.farm(handler="slow_square", management="passive", tasks=list(range(6))))
        await m.suspend("test")
        await asyncio.sleep(0.5)
        done_while_suspended = m.completed
        await asyncio.sleep(0.5)
        assert m.completed == done_while_suspended
        await m.resume("test")
        await m.close()
        await probe.succeeded(m, timeout=10)
        assert m.completed == 6

    async def test_contract_hot_swap(self, probe):
        m = await probe.submit(specs.farm(tasks=[1], management="active", max_workers=5))
        pin = schemas.ContractSpec(source='rule "pin" when Job( workers != 4 ) then scale(4); end')
        await m.set_contract(*compiler.compile_contract("farm", pin))
        await probe.wait(lambda: m.workers == 4)
        await m.cancel()
        assert m.status == schemas.JobStatus.CANCELLED


class FailuresTest:
    async def test_retries_then_success(self, probe):
        m = await probe.succeeded(
            await probe.submit(
                specs.farm(handler="flaky", tasks=list(range(10)), close_when_done=True, max_retries=2)
            )
        )
        assert (m.retried, m.completed, m.failed) == (5, 10, 0)

    async def test_permanent_failures_are_reported(self, probe):
        m = await probe.succeeded(
            await probe.submit(
                specs.farm(handler="always_fail", tasks=[1, 2], close_when_done=True, max_retries=1)
            )
        )
        tasks = await probe.tasks(m)
        assert {t.status for t in tasks} == {"failed"} and all(t.attempts == 2 for t in tasks)
        assert "ValueError" in tasks[0].error

    async def test_crashed_worker_task_is_redelivered(self, probe, engine):
        m = await probe.submit(
            specs.farm(
                handler="slow_square",
                workers=2,
                management="passive",
                tasks=list(range(12)),
                close_when_done=True,
            )
        )
        await asyncio.sleep(0.2)
        assert await engine.backend.kill_one(m.id)
        await probe.succeeded(m, timeout=15)
        assert await probe.results(m) == [i * i for i in range(12)]

    async def test_lost_result_is_healed(self, probe, engine, monkeypatch):
        """A result that never reaches the engine must not leave the job hanging."""
        # pylint: disable=protected-access  # swaps the manager's result subscription
        monkeypatch.setattr("aje.application.runtime.job.ORPHAN_GRACE", 0.5)
        m = await probe.submit(specs.farm(management="passive"))
        deliver, dropped = m.on_message, []

        async def lossy(msg):
            if msg.get("type") == "result" and not dropped:
                dropped.append(msg["task_id"])
                return
            await deliver(msg)

        m.on_message = lossy
        await m._sub.cancel()
        m._sub = await engine.broker.consume_results(m.id, m.on_message)
        await m.submit_tasks([7, 8, 9])
        await m.close()
        await probe.succeeded(m, timeout=15)
        assert dropped and await probe.results(m) == [49, 64, 81]
        assert await probe.events(m, "task.redispatched")


class DataParallelTest:
    @pytest.mark.parametrize(
        "mode, handler, items, expected",
        [
            ("sum", "sum_squares", list(range(50)), sum(i * i for i in range(50))),
            ("concat", "upper", list("abcdefghij"), list("ABCDEFGHIJ")),
            ("merge", "word_count", ["a b", "b c", "c c"], {"a": 1, "b": 2, "c": 3}),
        ],
    )
    async def test_gather(self, probe, mode, handler, items, expected):
        m = await probe.succeeded(
            await probe.submit(
                specs.dp(handler=handler, gather=mode, partitions=3, items=items, close_when_done=True)
            )
        )
        [batch] = await probe.batches(m)
        assert batch.result == expected and batch.partitions == 3

    async def test_repartitions_between_batches(self, probe):
        rule = 'rule "fit" when $j : Job( partitions < workers ) then set_partitions($j.workers * 2); end'
        m = await probe.submit(specs.dp(workers=3, max_workers=3, partitions=1, contract={"source": rule}))
        await probe.wait(lambda: m.partitions == 6)
        _, parts = await m.submit_batch(list(range(30)))
        assert parts == 6

    async def test_batch_fails_when_a_partition_fails(self, probe):
        m = await probe.submit(specs.dp(handler="always_fail", max_retries=0, items=[1, 2, 3], partitions=3))
        await probe.wait(lambda: m.batches_failed == 1)
        [batch] = await probe.batches(m)
        assert batch.status == "failed" and "ValueError" in batch.error


class GroupsTest:
    async def test_budget_escalation_and_rebalancing(self, probe):
        # the idle member never releases workers by itself: only the group manager can reclaim them
        hold = {"globals": {"idle_after": 100000, "low_utilisation": 0.0}}
        grow = {"globals": {"backlog_per_worker": 1}}
        g = await probe.submit_group(
            specs.group(
                specs.farm("idle", workers=3, max_workers=4, contract=hold),
                specs.farm(
                    "busy",
                    handler="slow_square",
                    workers=1,
                    max_workers=4,
                    contract=grow,
                    tasks=list(range(60)),
                ),
                max_workers=4,
                contract={"globals": {"reclaim_idle_after": 0.5}},
            )
        )
        busy, idle = sorted(g.members(), key=lambda m: m.name)
        assert g.used_workers() == 4
        await probe.wait(lambda: busy.workers >= 3, timeout=15)
        assert g.used_workers() <= 4 and idle.workers == 1
        assert await probe.events(busy, "job.escalated")

    async def test_budget_never_exceeded_and_shrinks_on_change(self, probe):
        g = await probe.submit_group(
            specs.group(
                specs.farm("a", workers=3, max_workers=6, management="passive"),
                specs.farm("b", workers=3, max_workers=6, management="passive"),
                max_workers=5,
                management="passive",
            )
        )
        a, b = sorted(g.members(), key=lambda m: m.name)
        assert (a.workers, b.workers) == (3, 2)
        assert await b.set_workers(6, "api") == 2
        await g.set_budget(3)
        assert g.used_workers() <= 3

    async def test_cancel_cancels_every_member(self, probe):
        g = await probe.submit_group(specs.group(specs.farm("a"), specs.dp("b")))
        assert set((await g.for_members("cancel")).values()) == {"ok"} and g.status == "cancelled"


# ---------------------------------------------------------------- invariance property
@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
@hypothesis.settings(max_examples=8, deadline=None, suppress_health_check=list(hypothesis.HealthCheck))
@hypothesis.given(
    items=st.lists(st.integers(-50, 50), min_size=1, max_size=40),
    partitions=st.integers(1, 8),
    scaling=st.lists(st.integers(1, 6), min_size=1, max_size=4),
)
def test_result_is_independent_of_management(schema, database_url, amqp_url, items, partitions, scaling):
    """Changing workers and partitions (the only knobs a contract can touch) never changes results."""

    async def scenario():
        await schema.upgrade()  # idempotent; job ids keep examples apart
        settings = test_engine.make_settings(
            database_url, amqp_url, monitor_interval=0.05, heartbeat_interval=0.1
        )
        async with test_engine.running_engine(settings, heartbeat=0.05) as eng:
            probe = probe_lib.EngineProbe(eng)
            d = await probe.submit(
                specs.dp(gather="sum", partitions=partitions, management="passive", max_workers=6)
            )
            f = await probe.submit(
                specs.farm(handler="random_sleep_square", management="passive", max_workers=6)
            )
            batch_ids = []
            for n in scaling:
                await d.set_workers(n, "prop")
                await f.set_workers(n, "prop")
                await d.set_partitions(n + partitions, "prop")
                batch_ids.append((await d.submit_batch(items))[0])
                await f.submit_tasks(items)
                await asyncio.sleep(0.02)
            await d.close()
            await f.close()
            await probe.succeeded(d, timeout=30)
            await probe.succeeded(f, timeout=30)
            expected = sum(v * v for v in items)
            for bid in batch_ids:
                assert (await probe.batch(bid)).result == expected
            assert await probe.results(f) == sorted(v * v for v in items * len(scaling))

    asyncio.run(scenario())
