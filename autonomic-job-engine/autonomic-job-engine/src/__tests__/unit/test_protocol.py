"""Wire protocol: msgpack message codec and queue naming."""

import json

import pytest

from aje_worker import protocol

TASK = {
    "type": "task",
    "job_id": "abc",
    "task_id": "t1",
    "attempt": 1,
    "payload": {"xs": [1, 2.5, None, True], "name": "ünïcode", "blob": b"\x00\xff"},
    "enqueued_at": 1700000000.123,
}


class CodecTest:
    def test_round_trip_keeps_types_including_bytes(self):
        assert protocol.unpack(protocol.pack(TASK)) == TASK

    def test_is_smaller_than_json(self):
        message = {k: v for k, v in TASK.items() if k != "payload"} | {"payload": list(range(200))}
        assert len(protocol.pack(message)) < len(json.dumps(message).encode())

    @pytest.mark.parametrize("body", [b"{not msgpack", protocol.pack({"a": 1})[:-1], b"\x93\x01\x02\x03"])
    def test_rejects_anything_but_a_map(self, body):
        with pytest.raises(ValueError):
            protocol.unpack(body)


class OutputQueueTest:
    @pytest.mark.parametrize("output_id", ["squares", "nightly.v2", "a", "0f3c" * 8, "x_y-z"])
    def test_valid_ids(self, output_id):
        assert protocol.output_queue(output_id) == f"aje.output.{output_id}"

    @pytest.mark.parametrize("output_id", ["", ".hidden", "with space", "a/b", "x" * 101])
    def test_invalid_ids(self, output_id):
        with pytest.raises(ValueError):
            protocol.output_queue(output_id)
