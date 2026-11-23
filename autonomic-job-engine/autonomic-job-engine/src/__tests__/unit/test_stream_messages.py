"""Stream building blocks with no I/O: resume points, SSE messages, the replay cursor."""

import pytest
from sse_starlette import sse

from aje.application.services import streams


class ResumePointTest:
    @pytest.mark.parametrize(
        "last_event_id, after_id, expected",
        [
            ("7", None, 7),
            ("7", 3, 7),  # a reconnecting EventSource wins over the original query
            (None, 3, 3),
            ("", 4, 4),
            ("not-a-number", 3, 3),
            ("-5", None, 0),
            (None, None, None),
        ],
    )
    def test_last_event_id_then_after_id(self, last_event_id, after_id, expected):
        assert streams.resume_point(last_event_id, after_id) == expected


class MessageTest:
    def test_stored_events_carry_their_id(self):
        assert streams.message("event", {"a": 1}, 5) == {"event": "event", "data": '{"a": 1}', "id": "5"}

    def test_other_messages_have_no_id(self):
        assert "id" not in streams.message("metrics", {"workers": 2})

    def test_the_id_reaches_the_wire(self):
        encoded = sse.ServerSentEvent(**streams.message("event", {"id": 5}, 5)).encode()
        assert b"id: 5" in encoded and b"event: event" in encoded


class CursorTest:
    def test_accepts_newer_events_and_advances(self):
        cursor = streams.Cursor(10)
        assert cursor.accept({"id": 11}) and cursor.last == 11

    @pytest.mark.parametrize("event_id", [10, 3])
    def test_skips_events_already_sent(self, event_id):
        assert not streams.Cursor(10).accept({"id": event_id})

    def test_delivers_events_that_could_not_be_stored(self):
        cursor = streams.Cursor(10)
        assert cursor.accept({"id": None}) and cursor.last == 10
