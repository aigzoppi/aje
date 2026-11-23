"""Event bus subscriptions (in-memory queues, no I/O): filters and overflow."""

import pytest

from aje.application.runtime import events


class SubscriptionTest:
    @pytest.mark.parametrize(
        "job_id, group_id, message_job, message_group, expected",
        [
            (None, None, "j1", None, True),
            ("j1", None, "j1", "g", True),
            ("j1", None, "j2", None, False),
            (None, "g", "j1", "g", True),  # a group subscription sees its members' events
            (None, "g", "j1", None, False),
        ],
    )
    def test_filters(self, job_id, group_id, message_job, message_group, expected):
        sub = events.Subscription(job_id, group_id, notifications=False, maxsize=10)
        assert sub.matches(message_job, message_group) is expected

    def test_queues_channel_and_message(self):
        sub = events.Subscription(None, None, notifications=True, maxsize=10)
        sub.push(events.TASK, {"kind": "task.done"})
        assert sub.queue.get_nowait() == (events.TASK, {"kind": "task.done"})

    def test_overflow_is_flagged_instead_of_dropping_silently(self):
        sub = events.Subscription(None, None, notifications=False, maxsize=2)
        for i in range(4):
            sub.push(events.EVENT, {"id": i})
        assert sub.overflowed
        assert [sub.queue.get_nowait()[1]["id"] for _ in range(2)] == [0, 1]
        sub.push(events.EVENT, {"id": 9})  # nothing is queued once overflowed
        assert sub.queue.empty()
