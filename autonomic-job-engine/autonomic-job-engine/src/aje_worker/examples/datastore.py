"""PostgreSQL as the data plane: RabbitMQ carries only references, data stays in the database.

    python -m aje_worker aje_worker.examples.datastore:power_rows

Job context:
    {"power": 2,                                    # free-form parameter
     "datastore": {"input": "dataplane.points",     # (id bigint primary key, x double precision)
                   "output": "dataplane.results",   # (id bigint primary key, y double precision, worker text)
                   "dsn_env": "AJE_DATASTORE_DSN"}} # the DSN itself is in the worker environment / a Secret

Task payload: an id range ``{"from": 0, "to": 1000}`` (``to`` exclusive), see ``id_ranges``.
Result: ``{"rows": <rows written>}``. A few bytes per task, however big the range is.

Writes are upserts, so a redelivered task (worker crash, scale-down) is harmless: running
it twice gives the same output. That idempotency is what makes the data plane safe under
autonomic management.
"""

from __future__ import annotations

from typing import Any

from psycopg import sql

from aje_worker import runtime


def id_ranges(start: int, stop: int, size: int) -> list[dict[str, int]]:
    """Task payloads covering [start, stop) in chunks of ``size`` ids."""
    return [{"from": lo, "to": min(lo + size, stop)} for lo in range(start, stop, size)]


def power_rows(payload: dict[str, Any], ctx: runtime.TaskContext) -> dict[str, int]:
    """Reads rows ``[from, to)`` of the input table and writes ``y = x ** power`` to the output.

    Args:
        payload: The id range: ``{"from": ..., "to": ...}``.
        ctx: Task context; its job context holds ``power`` and the datastore tables.

    Returns:
        ``{"rows": n}``: the number of rows written (the data never goes through RabbitMQ).
    """
    store = ctx.job_context.require_datastore()
    power = float(ctx.job_context.get("power", 2))
    lo, hi = int(payload["from"]), int(payload["to"])
    read = sql.SQL("SELECT id, x FROM {} WHERE id >= %s AND id < %s ORDER BY id").format(store.table("input"))
    write = sql.SQL(
        "INSERT INTO {} (id, y, worker) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET y = EXCLUDED.y, worker = EXCLUDED.worker"
    ).format(store.table("output"))
    with store.connect() as conn:  # commits on success, rolls back on error
        with conn.cursor() as cur:
            cur.execute(read, (lo, hi))
            rows = cur.fetchall()
            cur.executemany(write, [(i, x**power, ctx.worker_id) for i, x in rows])
    return {"rows": len(rows)}
