"""Data-parallel pattern: scatter a batch into partitions, gather the partial results.

Workers pull partitions from the shared queue, so the number of workers can be
changed at any time without affecting results (workers are stateless). The
partition count is a second knob; it only applies to batches submitted after
the change, i.e. at a batch boundary.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pydantic

from aje.application.runtime import job
from aje.domain import dsl
from aje.infrastructure.persistence import db

log = logging.getLogger("aje.data_parallel")

MAX_PARTITIONS = 1024


def split(items: list[Any], parts: int) -> list[list[Any]]:
    """Contiguous, order-preserving split into ``parts`` near-equal slices."""
    parts = max(1, min(parts, len(items)))
    k, m = divmod(len(items), parts)
    out, start = [], 0
    for i in range(parts):
        end = start + k + (1 if i < m else 0)
        out.append(items[start:end])
        start = end
    return out


def gather(mode: str, results: list[Any]) -> Any:
    """Combines partition results, in partition order.

    Args:
        mode: ``list`` (as is), ``concat`` (flatten one level), ``sum``, or ``merge``
            (dicts: numbers are added, other values overwritten).
        results: One result per partition.

    Returns:
        The gathered value.

    Raises:
        ValueError: Unknown mode, or ``merge`` over something that is not a dict.
    """
    if mode == "list":
        return results
    if mode == "concat":
        out: list[Any] = []
        for r in results:
            if isinstance(r, list):
                out.extend(r)
            else:
                out.append(r)
        return out
    if mode == "sum":
        total: Any = 0
        for r in results:
            total = total + r
        return total
    if mode == "merge":
        merged: dict[str, Any] = {}
        for r in results:
            if not isinstance(r, dict):
                raise ValueError("gather 'merge' expects every partition to return an object")
            for k, v in r.items():
                if k in merged and isinstance(v, (int, float)) and isinstance(merged[k], (int, float)):
                    merged[k] += v
                else:
                    merged[k] = v
        return merged
    raise ValueError(f"unknown gather mode {mode!r}")


class BatchState(pydantic.BaseModel):
    """An in-flight batch: the partial results and service times gathered so far."""

    id: str
    size: int
    partitions: int
    started: float
    results: dict[int, Any] = pydantic.Field(default_factory=dict)
    service: dict[int, float] = pydantic.Field(default_factory=dict)
    failed: str | None = None
    done: bool = False


class DataParallelManager(job.JobManager):
    """Manager of a data-parallel job: scatters batches into partitions and gathers results.

    Adds a second knob to the farm's worker count: the number of partitions per batch,
    applied from the next batch on.
    """

    catalog = dsl.DATA_PARALLEL

    def __init__(self, engine, row: db.JobRow, rulebase):
        super().__init__(engine, row, rulebase)
        self.partitions = row.partitions or self.spec.partitions
        self.batches: dict[str, BatchState] = {}
        self.batches_done = 0
        self.batches_failed = 0
        self.last_batch_time: float | None = None
        self.partition_skew: float | None = None

    async def load_state(self) -> None:
        await super().load_state()
        rows = await self.store.find(db.BatchRow, {"job_id": self.id, "status": "running"})
        for b in rows:
            st = BatchState(id=b.id, size=b.size, partitions=b.partitions, started=b.created_at.timestamp())
            done = await self.store.find(db.TaskRow, {"batch_id": b.id, "status": "done"})
            for t in done:
                st.results[t.partition] = t.result
            self.batches[b.id] = st

    # ---------------------------------------------------------------- input
    async def submit_batch(self, items: list[Any], partitions: int | None = None) -> tuple[str, int]:
        """Scatters a batch: one task per partition.

        Args:
            items: The batch.
            partitions: Partitions for this batch; defaults to the job's current setting.

        Returns:
            The batch id and the number of partitions actually used.

        Raises:
            job.JobStateError: The job no longer accepts input.
        """
        self._check_accepting()
        p = max(1, min(partitions or self.partitions, len(items), MAX_PARTITIONS))
        bid = job.new_id(16)
        now = time.time()
        slices = split(items, p)
        self.batches[bid] = BatchState(id=bid, size=len(items), partitions=len(slices), started=now)
        await self.store.add(
            db.BatchRow(
                id=bid,
                job_id=self.id,
                status="running",
                size=len(items),
                partitions=len(slices),
                created_at=db.ts(now),
            )
        )
        entries = [
            (f"{bid}-{i}", job.TaskState(payload=sl, attempt=1, enqueued_at=now, batch_id=bid, partition=i))
            for i, sl in enumerate(slices)
        ]
        await self._dispatch(entries)
        await self.emit(
            "batch.scattered",
            f"batch {bid}: {len(items)} item(s) in {len(slices)} partition(s)",
            batch_id=bid,
            partitions=len(slices),
            size=len(items),
        )
        return bid, len(slices)

    async def submit_tasks(self, payloads):  # a farm-only operation
        raise ValueError("data_parallel jobs take batches: POST /v1/jobs/{id}/batches")

    # ---------------------------------------------------------------- gather
    async def publish_task_output(
        self, kind: str, task_id: str, st: job.TaskState, msg: dict, **fields: Any
    ) -> None:
        """Partitions are internal: only gathered batches are published (see on_task_done)."""
        del kind, task_id, st, msg, fields  # Unused.

    async def on_task_done(self, task_id: str, st: job.TaskState, msg: dict) -> None:
        b = self.batches.get(st.batch_id)
        if b is None or b.done:
            return
        b.results[st.partition] = msg.get("result")
        b.service[st.partition] = max(0.0, float(msg.get("finished_at", 0)) - float(msg.get("started_at", 0)))
        await self.store.update(db.BatchRow, b.id, completed_partitions=len(b.results))
        if len(b.results) < b.partitions or b.failed:
            return
        b.done = True
        duration = time.time() - b.started
        try:
            value = gather(self.spec.gather, [b.results[i] for i in range(b.partitions)])
        except Exception as e:  # pylint: disable=broad-exception-caught  # gather runs user-chosen merges: any failure fails the batch
            await self._fail_batch(b, f"gather failed: {e}")
            return
        try:
            await self.publish_output(
                "batch.result",
                f"{self.id}:{b.id}",
                batch_id=b.id,
                partitions=b.partitions,
                size=b.size,
                duration=duration,
                result=value,
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            # An output that cannot be delivered fails the batch visibly instead of vanishing.
            await self._fail_batch(b, f"output not published: {e}")
            return
        services = [s for s in b.service.values() if s > 0]
        self.partition_skew = round(max(services) / min(services), 3) if len(services) > 1 else None
        self.last_batch_time = round(duration, 3)
        self.batches_done += 1
        del self.batches[b.id]
        await self.store.update(
            db.BatchRow, b.id, status="done", result=value, finished_at=db.utcnow(), duration=duration
        )
        await self.emit(
            "batch.done",
            f"batch {b.id} gathered in {duration:.2f}s",
            batch_id=b.id,
            duration=round(duration, 3),
            partitions=b.partitions,
            skew=self.partition_skew,
        )

    async def on_task_failed(self, task_id: str, st: job.TaskState, error: str) -> None:
        b = self.batches.get(st.batch_id)
        if b is not None and not b.done:
            await self._fail_batch(b, f"partition {st.partition} failed: {error}")

    async def _fail_batch(self, b: BatchState, error: str) -> None:
        """Fails a batch: drops its remaining partitions, records and publishes the failure."""
        b.done = True
        b.failed = error
        self.batches_failed += 1
        self.batches.pop(b.id, None)
        # drop the batch's remaining partitions from the outstanding set
        for tid in [t for t, s in self.tasks.items() if s.batch_id == b.id]:
            del self.tasks[tid]
        await self.store.update(db.BatchRow, b.id, status="failed", error=error, finished_at=db.utcnow())
        try:
            await self.publish_output("batch.failed", f"{self.id}:{b.id}", batch_id=b.id, error=error)
        except Exception:  # pylint: disable=broad-exception-caught
            # Best effort: the failure is stored on the batch and announced as an event too.
            log.exception("could not publish the failure of batch %s", b.id)
        await self.emit("batch.failed", f"batch {b.id} failed: {error}", batch_id=b.id)

    # ---------------------------------------------------------------- facts & knobs
    def pattern_metrics(self, now: float) -> dict[str, Any]:
        return {
            "partitions": self.partitions,
            "pending_batches": len(self.batches),
            "pending_partitions": sum(b.partitions - len(b.results) for b in self.batches.values()),
            "batches_done": self.batches_done,
            "batches_failed": self.batches_failed,
            "last_batch_time": self.last_batch_time,
            "partition_skew": self.partition_skew,
        }

    def job_fact(self) -> dict[str, Any]:
        return {**super().job_fact(), "partitions": self.partitions}

    def work_pending(self) -> bool:
        return bool(self.tasks) or bool(self.batches)

    async def apply_pattern_action(self, call: dsl.ActionCall, source: str) -> None:
        if call.name == "set_partitions":
            await self.set_partitions(int(call.args[0]), source)
        else:
            await super().apply_pattern_action(call, source)

    async def set_partitions(self, n: int, source: str) -> int:
        """Sets the partition count for batches submitted from now on.

        Args:
            n: The requested count (clamped to 1..MAX_PARTITIONS).
            source: Who asks, for the event.

        Returns:
            The partition count in effect.
        """
        n = max(1, min(MAX_PARTITIONS, int(n)))
        if n == self.partitions:
            return n
        old, self.partitions = self.partitions, n
        await self.store.update(db.JobRow, self.id, partitions=n)
        await self.emit(
            "job.repartitioned",
            f"partitions {old} -> {n} by {source} (from the next batch)",
            old=old,
            new=n,
            source=source,
        )
        return n

    def counts(self) -> dict[str, int]:
        return {
            **super().counts(),
            "batches_pending": len(self.batches),
            "batches_done": self.batches_done,
            "batches_failed": self.batches_failed,
        }
