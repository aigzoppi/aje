"""Wire protocol shared by the engine, the workers and output consumers (RabbitMQ).

Per job (internal, deleted when the job ends):
  aje.<job>.tasks     durable classic queue   engine -> workers  (prefetch 1, manual ack)
  aje.<job>.results   durable classic queue   workers -> engine  (results + heartbeats)
  aje.<job>.control   fanout exchange         engine -> every worker (drain)

Per output stream (public, never deleted by the engine):
  aje.output.<id>     durable classic queue   engine -> downstream consumers
  The id is independent of any job: one stream may collect the outputs of many jobs
  (e.g. every run of a schedule), so every message carries its ``job_id``.

Every message body is msgpack (content type ``application/vnd.msgpack``): a map of
the fields below. msgpack encodes any JSON value plus raw bytes, in less space and
time than JSON, with libraries for every mainstream language.

  task      {"type": "task", "job_id", "task_id", "attempt", "batch_id"?, "partition"?,
             "payload", "enqueued_at"}
  result    {"type": "result", "job_id", "task_id", "attempt", "batch_id"?, "partition"?,
             "ok", "result"?, "error"?, "worker", "started_at", "finished_at"}
  heartbeat {"type": "heartbeat", "job_id", "worker", "busy", "leaving", "ts"}
  control   {"cmd": "drain"}  finish the current task, stop consuming, exit 0
  output    {"v": 1, "kind", "output_id", "job_id", "job_name", "pattern", "ts", ...}
            kind task.result   task_id, attempt, worker, started_at, finished_at, result
                 task.failed   task_id, attempt, worker, error
                 batch.result  batch_id, partitions, size, duration, result
                 batch.failed  batch_id, error
                 job.finished  status, message, counts
            AMQP properties: message_id (idempotency key), type = kind,
            headers {"job_id", "kind"}.
"""

from __future__ import annotations

import re
from typing import Any

import msgpack

CONTENT_TYPE = "application/vnd.msgpack"
OUTPUT_VERSION = 1
OUTPUT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
_OUTPUT_ID = re.compile(OUTPUT_ID_PATTERN)


def pack(message: dict[str, Any]) -> bytes:
    """Encodes a message body (msgpack; ``bytes`` values stay binary)."""
    return msgpack.packb(message, use_bin_type=True)


def unpack(body: bytes) -> dict[str, Any]:
    """Decodes a message body.

    Raises:
        ValueError: The body is not a msgpack map.
    """
    try:
        message = msgpack.unpackb(body, raw=False)
    except (msgpack.UnpackException, ValueError) as e:
        raise ValueError(f"not a msgpack message: {e}") from None
    if not isinstance(message, dict):
        raise ValueError(f"expected a msgpack map, got {type(message).__name__}")
    return message


def tasks_queue(job_id: str) -> str:
    return f"aje.{job_id}.tasks"


def results_queue(job_id: str) -> str:
    return f"aje.{job_id}.results"


def control_exchange(job_id: str) -> str:
    return f"aje.{job_id}.control"


def output_queue(output_id: str) -> str:
    """The queue of an output stream.

    Raises:
        ValueError: ``output_id`` is not 1-100 letters, digits, ``.``, ``_`` or ``-``
            (starting with a letter or digit).
    """
    if not _OUTPUT_ID.match(output_id):
        raise ValueError(f"invalid output id {output_id!r}")
    return f"aje.output.{output_id}"


ENV_AMQP_URL = "AJE_AMQP_URL"
ENV_JOB_ID = "AJE_JOB_ID"
ENV_WORKER_ID = "AJE_WORKER_ID"
ENV_HEARTBEAT = "AJE_HEARTBEAT_INTERVAL"
ENV_TERM_GRACE = "AJE_TERM_GRACE"
ENV_JOB_CONTEXT = "AJE_JOB_CONTEXT"  # the job's jobcontext as JSON (see aje_worker.context)

EXIT_DRAINED = 0  # normal end of job: counts as success
EXIT_TERMINATED = 143  # scale-down / suspension: must NOT count as success (work-queue Job semantics)
