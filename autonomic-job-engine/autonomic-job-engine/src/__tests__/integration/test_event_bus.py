"""Event bus on the real Store: persisted events and opt-in live notifications."""

import pytest

from aje.application.runtime import events
from aje.infrastructure.persistence import db


@pytest.fixture(name="bus")
def bus_fixture(store) -> events.EventBus:
    return events.EventBus(store)


def drain(sub: events.Subscription) -> list[tuple[str, dict]]:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


class EventsTest:
    async def test_are_persisted_then_pushed_with_their_id(self, bus, store):
        everything = bus.subscribe()
        one_job = bus.subscribe(job_id="j1")
        await bus.emit("job.started", "go", job_id="j1")
        await bus.emit("job.started", "go", job_id="j2", data={"k": [1, None]})

        rows = await store.find(db.EventRow, order_by="id")
        assert [(r.job_id, r.kind, r.data) for r in rows] == [
            ("j1", "job.started", None),
            ("j2", "job.started", {"k": [1, None]}),
        ]
        pushed = drain(everything)
        assert [channel for channel, _ in pushed] == [events.EVENT, events.EVENT]
        assert [e["id"] for _, e in pushed] == [r.id for r in rows]
        assert [e["id"] for _, e in drain(one_job)] == [rows[0].id]

    async def test_an_unstored_event_is_still_an_event(self, store):
        # finding 9: an event the store refused keeps channel "event" (its id is None)
        bus = events.EventBus(store)
        await store.close()  # every write now fails
        sub = bus.subscribe(notifications=True)
        await bus.emit("job.started", "go", job_id="j1")
        pushed = drain(sub)
        assert len(pushed) == 1
        channel, event = pushed[0]
        assert channel == events.EVENT and event["id"] is None

    async def test_unsubscribed_queues_receive_nothing(self, bus):
        sub = bus.subscribe()
        bus.unsubscribe(sub)
        await bus.emit("job.started", "go", job_id="j1")
        assert sub.queue.empty()


class NotificationsTest:
    async def test_are_opt_in_and_never_stored(self, bus, store):
        plain = bus.subscribe()
        assert not bus.wants_notifications("j1")
        live = bus.subscribe(notifications=True)
        assert bus.wants_notifications("j1")
        bus.notify("task.done", job_id="j1", data={"task_id": "t"})
        assert not await store.find(db.EventRow) and plain.queue.empty()
        pushed = drain(live)
        assert len(pushed) == 1
        channel, msg = pushed[0]
        assert channel == events.TASK and msg["kind"] == "task.done" and msg["data"] == {"task_id": "t"}
        bus.unsubscribe(live)
        assert not bus.wants_notifications("j1")
