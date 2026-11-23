"""Per-job autonomic manager: Monitor -> Analyse -> Plan -> Execute.

The manager owns the *control interface* of one job (scale, suspend, resume,
and for data-parallel jobs re-partition) and, when management is ``active``,
runs the job's contract over the facts it observes. In ``passive`` mode only
the control interface is used, driven by the API.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Any, TYPE_CHECKING
import uuid

import pydantic

from aje.application import schemas
from aje.application.ports import backend as backend_lib
from aje.application.runtime import metrics as metrics_lib
from aje.application.runtime import prom
from aje.domain import dsl
from aje.domain import errors
from aje.infrastructure.persistence import db
from aje_worker import protocol

if TYPE_CHECKING:
    from aje.application.runtime import engine as engine_lib

log = logging.getLogger("aje.manager")

ESCALATION_TTL = 30.0
DRAIN_TIMEOUT = 120.0
ORPHAN_GRACE = 30.0


def new_id(n: int = 12) -> str:
    return uuid.uuid4().hex[:n]


class TaskState(pydantic.BaseModel):
    payload: Any
    attempt: int
    enqueued_at: float
    batch_id: str | None = None
    partition: int | None = None


class JobManager:
    """Autonomic manager of one farm job; patterns subclass it (see DataParallelManager).

    It mirrors the job's state in memory (outstanding tasks, counters, worker heartbeats),
    persists every change, and runs the MAPE loop: each ``interval`` it monitors the queue
    and the workers, supervises the lifecycle, heals orphaned tasks and, when management is
    active, runs the contract and executes the actions it fires.

    Attributes:
        catalog: Facts and actions the job's contracts may use.
        tasks: Outstanding tasks: task id -> state (payload, attempt, ...).
        workers: Worker count the job asks the backend for.
        status: Current lifecycle status.
        escalations: Open requests to the group manager, by reason.
    """

    catalog: dsl.Catalog = dsl.FARM

    def __init__(self, engine: engine_lib.AutonomicEngine, row: db.JobRow, rulebase: dsl.RuleBase | None):
        s = engine.settings
        self.engine = engine
        self.id = row.id
        self.name = row.name
        self.spec = schemas.JobCreate.model_validate(row.spec)
        self.pattern = row.pattern
        self.group_id = row.group_id
        self.status = schemas.JobStatus(row.status)
        self.management = row.management
        self.workers = row.workers
        self.closed = row.closed
        self.contract_info = row.contract or {}
        self.output_id = self.spec.output_queue
        self.rulebase = rulebase
        self.interval = self.spec.monitor_interval or s.monitor_interval
        self.window = metrics_lib.MetricsWindow(self.spec.monitor_window or s.monitor_window)
        self.hb_interval = s.heartbeat_interval

        self.tasks: dict[str, TaskState] = {}
        self.submitted = self.completed = self.failed = self.retried = 0
        self.heartbeats: dict[str, tuple[float, bool]] = {}
        self.backend_status = backend_lib.BackendStatus()
        self.started_at = time.time()
        self.last_activity = self.started_at
        self.last_metrics: dict[str, Any] | None = None
        self.history: collections.deque = collections.deque(maxlen=720)
        self.escalations: dict[str, dict[str, Any]] = {}
        self.drain_started: float | None = None
        self._orphans_since: float | None = None
        self.message: str | None = None

        self._loop_task: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        self._sub = None
        self._lock = asyncio.Lock()
        self._labels = (self.id, self.name, self.pattern)

    # ================================================================ lifecycle
    @property
    def store(self):
        return self.engine.store

    @property
    def broker(self):
        return self.engine.broker

    @property
    def backend(self):
        return self.engine.backend

    async def emit(self, kind: str, message: str, **data) -> None:
        await self.engine.bus.emit(kind, message, job_id=self.id, group_id=self.group_id, data=data or None)

    def worker_spec(self) -> backend_lib.WorkerSpec:
        w = self.spec.worker
        return backend_lib.WorkerSpec(
            job_id=self.id,
            job_name=self.name,
            pattern=self.pattern,
            image=w.image,
            command=w.command,
            args=w.args,
            env=w.env,
            resources=w.resources.model_dump() if w.resources else {},
            node_selector=w.node_selector,
            service_account=w.service_account,
            image_pull_policy=w.image_pull_policy,
            backoff_limit=w.backoff_limit,
            labels=self.spec.labels,
            job_context=self.spec.jobcontext.model_dump(mode="json", exclude_none=True),
        )

    async def start(self, new: bool) -> None:
        """Starts managing the job: queues, result consumer, workers and the MAPE loop.

        Args:
            new: True for a job just submitted (workers are created); False for a job
                recovered after an engine restart (state is reloaded from the database).
        """
        await self.broker.declare_job(self.id)
        await self._declare_output()
        if not new:
            await self.load_state()  # before consuming results, or early results would be dropped
        self._sub = await self.broker.consume_results(self.id, self.on_message)
        if new:
            try:
                await self.backend.create(self.worker_spec(), self.workers)
            except Exception as e:  # pylint: disable=broad-exception-caught  # any backend failure fails the job with its message
                await self._finish(schemas.JobStatus.FAILED, f"could not start workers: {e}")
                return
            await self._set_status(schemas.JobStatus.RUNNING)
            await self.emit(
                "job.started",
                f"{self.pattern} job started with {self.workers} worker(s)",
                workers=self.workers,
                management=self.management,
            )
        else:
            await self._recover()
        self._loop_task = asyncio.create_task(self._loop(), name=f"aje-manager-{self.id}")

    async def _declare_output(self) -> None:
        """Declares the job's output stream; jobs stored before streams existed get one now."""
        if self.output_id is None:
            self.output_id = new_id(32)
            self.spec.output_queue = self.output_id
            await self.store.update(db.JobRow, self.id, spec=self.spec.model_dump(mode="json"))
        await self.broker.declare_output(self.output_id)

    async def publish_output(self, kind: str, message_id: str, **fields: Any) -> None:
        """Publishes one output of the job to its output stream (see ``aje_worker.protocol``).

        Args:
            kind: ``task.result``, ``task.failed``, ``batch.result``, ``batch.failed`` or
                ``job.finished``.
            message_id: Idempotency key for consumers.
            **fields: The kind-specific fields.
        """
        message = {
            "v": protocol.OUTPUT_VERSION,
            "kind": kind,
            "output_id": self.output_id,
            "job_id": self.id,
            "job_name": self.name,
            "pattern": self.pattern,
            "ts": time.time(),
            **fields,
        }
        await self.broker.publish_output(self.output_id, message, message_id)

    async def publish_task_output(
        self, kind: str, task_id: str, st: TaskState, msg: dict, **fields: Any
    ) -> None:
        """Publishes the final outcome of a farm task (patterns that gather override this)."""
        await self.publish_output(
            kind,
            f"{self.id}:{task_id}:{st.attempt}",
            task_id=task_id,
            attempt=st.attempt,
            worker=msg.get("worker"),
            **fields,
        )

    async def load_state(self) -> None:
        """Rebuilds outstanding tasks and counters from the stored task rows."""
        rows = await self.store.find(db.TaskRow, {"job_id": self.id})
        for r in rows:
            if r.status == "queued":
                self.tasks[r.id] = TaskState(
                    payload=r.payload,
                    attempt=r.attempts,
                    enqueued_at=r.submitted_at.timestamp(),
                    batch_id=r.batch_id,
                    partition=r.partition,
                )
        self.submitted = len(rows)
        self.completed = sum(1 for r in rows if r.status == "done")
        self.failed = sum(1 for r in rows if r.status == "failed")

    async def _recover(self) -> None:
        """Recreates missing workers of a recovered job, keeping a suspended job suspended."""
        st = await self.backend.status(self.id)
        if st.phase == "missing" and self.status in (
            schemas.JobStatus.RUNNING,
            schemas.JobStatus.DRAINING,
            schemas.JobStatus.PENDING,
        ):
            await self.backend.create(self.worker_spec(), self.workers)
            await self._set_status(schemas.JobStatus.RUNNING)
        elif st.phase == "missing" and self.status == schemas.JobStatus.SUSPENDED:
            await self.backend.create(self.worker_spec(), self.workers)
            await self.backend.suspend(self.id)
        await self.emit("job.recovered", f"manager recovered with {len(self.tasks)} outstanding task(s)")

    async def stop(self) -> None:
        """Stop managing (engine shutdown); workers and queues are left as they are."""
        if self._sub:
            await self._sub.cancel()
        await cancel_and_wait(self._loop_task)

    async def _set_status(self, status: schemas.JobStatus, message: str | None = None) -> None:
        self.status = status
        if message is not None:
            self.message = message
        values: dict[str, Any] = {"status": status.value, "message": self.message}
        if status in schemas.TERMINAL:
            values["finished_at"] = db.utcnow()
            values["counts"] = self.counts()  # the same keys the live job reports
        await self.store.update(db.JobRow, self.id, **values)

    async def _finish(self, status: schemas.JobStatus, message: str | None = None) -> None:
        """Ends the job: records the final status, stops the loop and releases resources.

        Workers and queues are deleted (succeeded Kubernetes jobs keep their pods for
        their logs, until the TTL removes them). Calling it on a finished job does nothing.

        Args:
            status: A terminal status.
            message: Why the job ended, shown in the API.
        """
        if self.status in schemas.TERMINAL:
            return
        await self._set_status(status, message)
        await self.emit(
            f"job.{status.value}",
            message or f"job {status.value}",
            completed=self.completed,
            failed=self.failed,
        )
        if self._loop_task and asyncio.current_task() is not self._loop_task:
            self._loop_task.cancel()
        if self._sub:
            await self._sub.cancel()
        try:
            if status != schemas.JobStatus.SUCCEEDED or self.backend.name != "kubernetes":
                await self.backend.delete(self.id)
        except Exception:  # pylint: disable=broad-exception-caught  # cleanup is best effort; the job is already finished
            log.exception("backend cleanup failed for %s", self.id)
        try:
            await self.publish_output(
                "job.finished",
                f"{self.id}:finished",
                status=status.value,
                message=self.message,
                counts=self.counts(),
            )
        except Exception:  # pylint: disable=broad-exception-caught
            # Best effort: the final status is stored and announced as an event too.
            log.exception("could not publish the end of job %s to its output stream", self.id)
        await self.broker.delete_job(self.id)
        prom.remove_job(self._labels)
        self.engine.on_job_finished(self)

    # ================================================================ input
    def _check_accepting(self) -> None:
        if self.status in schemas.TERMINAL or self.status == schemas.JobStatus.DRAINING:
            raise JobStateError(f"job is {self.status.value}; it no longer accepts input")
        if self.closed:
            raise JobStateError("job input is closed")

    async def _dispatch(self, entries: list[tuple[str, TaskState]]) -> None:
        """Persists new tasks, then publishes them to the job's task queue.

        Args:
            entries: (task id, state) pairs to dispatch.
        """
        now = time.time()
        rows = [
            db.TaskRow(
                id=tid,
                job_id=self.id,
                batch_id=st.batch_id,
                partition=st.partition,
                status="queued",
                attempts=st.attempt,
                payload=st.payload,
                submitted_at=db.ts(now),
            )
            for tid, st in entries
        ]
        await self.store.add(*rows)
        for tid, st in entries:
            self.tasks[tid] = st
        self.submitted += len(entries)
        self.window.add_arrivals(now, len(entries))
        self.last_activity = now
        await self.broker.publish_tasks(self.id, [self._message(tid, st) for tid, st in entries])

    def _message(self, task_id: str, st: TaskState) -> dict:
        return {
            "type": "task",
            "job_id": self.id,
            "task_id": task_id,
            "attempt": st.attempt,
            "batch_id": st.batch_id,
            "partition": st.partition,
            "payload": st.payload,
            "enqueued_at": st.enqueued_at,
        }

    async def submit_tasks(self, payloads: list[Any]) -> list[str]:
        self._check_accepting()
        now = time.time()
        entries = [(new_id(16), TaskState(payload=p, attempt=1, enqueued_at=now)) for p in payloads]
        await self._dispatch(entries)
        return [tid for tid, _ in entries]

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.store.update(db.JobRow, self.id, closed=True)
        await self.emit("job.closed", "input closed; the job finishes when outstanding work is done")

    # ================================================================ results
    async def on_message(self, msg: dict) -> None:
        """Handles a message from a worker: a heartbeat or a task result.

        A result completes its task, or schedules a retry (up to ``max_retries``), or fails
        it for good. Duplicate and stale deliveries (an older attempt) are ignored.

        Args:
            msg: The decoded message from the job's results queue.
        """
        kind = msg.get("type")
        if kind == "heartbeat":
            if msg.get("leaving"):
                self.heartbeats.pop(msg.get("worker"), None)
            else:
                self.heartbeats[msg.get("worker")] = (time.time(), bool(msg.get("busy")))
            return
        if kind != "result":
            return
        tid = msg.get("task_id")
        st = self.tasks.get(tid)
        if st is None or int(msg.get("attempt", 0)) != st.attempt:
            return  # duplicate or stale delivery
        now = time.time()
        self.last_activity = now
        started = float(msg.get("started_at") or now)
        finished = float(msg.get("finished_at") or now)
        ok = bool(msg.get("ok"))
        self.window.add_completion(
            metrics_lib.Completion(
                t=finished,
                latency=max(0.0, finished - st.enqueued_at),
                service=max(0.0, finished - started),
                ok=ok,
            )
        )
        prom.TASKS.labels(*self._labels, "ok" if ok else "error").inc()
        if self.heartbeats.get(msg.get("worker")):
            self.heartbeats[msg["worker"]] = (now, False)

        if ok:
            await self.publish_task_output(
                "task.result",
                tid,
                st,
                msg,
                started_at=started,
                finished_at=finished,
                result=msg.get("result"),
            )
            del self.tasks[tid]
            self.completed += 1
            await self.store.update(
                db.TaskRow,
                tid,
                status="done",
                result=msg.get("result"),
                error=None,
                worker=msg.get("worker"),
                started_at=db.ts(started),
                finished_at=db.ts(finished),
            )
            self._notify_task(
                "task.done", tid, st, msg, service=max(0.0, finished - started), result=msg.get("result")
            )
            await self.on_task_done(tid, st, msg)
            return

        error = msg.get("error") or "unknown error"
        if st.attempt <= self.spec.max_retries:
            st.attempt += 1
            self.retried += 1
            await self.store.update(
                db.TaskRow, tid, attempts=st.attempt, error=error, worker=msg.get("worker")
            )
            await self.broker.publish_tasks(self.id, [self._message(tid, st)])
            return
        await self.publish_task_output("task.failed", tid, st, msg, error=error)
        del self.tasks[tid]
        self.failed += 1
        await self.store.update(
            db.TaskRow,
            tid,
            status="failed",
            error=error,
            worker=msg.get("worker"),
            started_at=db.ts(started),
            finished_at=db.ts(finished),
        )
        await self.emit(
            "task.failed", f"task {tid} failed after {st.attempt} attempt(s): {error}", task_id=tid
        )
        self._notify_task("task.failed", tid, st, msg, error=error)
        await self.on_task_failed(tid, st, error)

    def _notify_task(self, kind: str, task_id: str, st: TaskState, msg: dict, **extra: Any) -> None:
        """Live (non-persisted) task update for dashboards; skipped when nobody listens."""
        bus = self.engine.bus
        if not bus.wants_notifications(self.id, self.group_id):
            return
        bus.notify(
            kind,
            job_id=self.id,
            group_id=self.group_id,
            data={
                "task_id": task_id,
                "worker": msg.get("worker"),
                "attempt": st.attempt,
                "batch_id": st.batch_id,
                "partition": st.partition,
                "counts": self.counts(),
                **extra,
            },
        )

    async def on_task_done(self, task_id: str, st: TaskState, msg: dict) -> None:
        """Hook for patterns (data-parallel gather)."""

    async def on_task_failed(self, task_id: str, st: TaskState, error: str) -> None:
        """Hook for patterns."""

    # ================================================================ MAPE loop
    async def _loop(self) -> None:
        while self.status not in schemas.TERMINAL:
            try:
                await self.tick()
            except Exception:  # pylint: disable=broad-exception-caught  # a failing tick must not kill the manager
                log.exception("manager tick failed for %s", self.id)
            await asyncio.sleep(self.interval)

    async def tick(self) -> None:
        """One MAPE iteration: monitor, supervise the lifecycle, heal, then run the contract."""
        async with self._lock:
            metrics = await self.monitor()
            if await self._lifecycle():
                return
            await self._heal_orphans(metrics)
            if (
                self.management == "active"
                and self.rulebase is not None
                and self.status in (schemas.JobStatus.RUNNING, schemas.JobStatus.SUSPENDED)
            ):
                firings = self.rulebase.run(self.facts(metrics))
                for err in set(self.rulebase.eval_errors):
                    await self.emit("rule.error", err)
                await self.execute(firings)
            self._expire_escalations()

    # ---- Monitor
    async def monitor(self) -> dict[str, Any]:
        """Collects the job's metrics: queue, workers, counters and the sliding window.

        Also refreshes the backend status, drops stale heartbeats, appends to the history
        and exports the Prometheus gauges.

        Returns:
            The metrics, as the ``Metrics`` fact the contract sees.
        """
        now = time.time()
        stats = await self.broker.queue_stats(self.id)
        try:
            self.backend_status = await self.backend.status(self.id)
        except Exception as e:  # pylint: disable=broad-exception-caught  # an unreachable backend is reported as unknown
            self.backend_status = backend_lib.BackendStatus(phase="unknown", message=str(e))
        live = {w: hb for w, hb in self.heartbeats.items() if now - hb[0] < 3 * self.hb_interval + 1}
        self.heartbeats = live
        outstanding = len(self.tasks)
        in_flight = max(0, outstanding - stats.depth)
        n_live = len(live)
        util = min(1.0, in_flight / n_live) if n_live else 0.0
        self.window.add_sample(now, n_live, util)
        m: dict[str, Any] = {
            "queue_depth": stats.depth,
            "consumers": stats.consumers,
            "in_flight": in_flight,
            "outstanding": outstanding,
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "retried": self.retried,
            "live_workers": n_live,
            "busy_workers": sum(1 for _, b in live.values() if b),
            "utilisation": round(util, 4),
            "idle_seconds": round(now - self.last_activity, 2),
            "elapsed": round(now - self.started_at, 2),
            "pod_failures": self.backend_status.failed,
        }
        m.update(self.window.snapshot(now, now - self.started_at))
        m.update(self.pattern_metrics(now))
        m["ts"] = now
        self.last_metrics = m
        self.history.append(
            {
                k: m[k]
                for k in (
                    "ts",
                    "queue_depth",
                    "outstanding",
                    "throughput",
                    "avg_latency",
                    "avg_utilisation",
                    "live_workers",
                )
            }
            | {"workers": self.workers}
        )
        self._export(m)
        return m

    def pattern_metrics(self, now: float) -> dict[str, Any]:
        """Metrics specific to the pattern (a hook; farms have none)."""
        del now  # Unused: farms have no pattern metrics.
        return {}

    def _export(self, m: dict[str, Any]) -> None:
        labels = self._labels
        prom.WORKERS.labels(*labels).set(self.workers)
        prom.ACTIVE.labels(*labels).set(self.backend_status.active)
        prom.QUEUE_DEPTH.labels(*labels).set(m["queue_depth"])
        prom.OUTSTANDING.labels(*labels).set(m["outstanding"])
        prom.THROUGHPUT.labels(*labels).set(m["throughput"] or 0)
        prom.LATENCY.labels(*labels).set(m["avg_latency"] or 0)
        prom.UTILISATION.labels(*labels).set(m["avg_utilisation"] or 0)

    # ---- lifecycle supervision (returns True when the job just ended)
    def work_pending(self) -> bool:
        return bool(self.tasks)

    async def _lifecycle(self) -> bool:
        """Moves the job through its lifecycle: running -> draining -> succeeded, or failed.

        A closed job with no work left asks its workers to exit (draining) and succeeds
        once they are gone (or after DRAIN_TIMEOUT). Failed workers fail the job.

        Returns:
            True when the job just ended.
        """
        bs = self.backend_status
        if bs.phase == "failed":
            reason = bs.message or "backoff limit reached"
            await self._finish(schemas.JobStatus.FAILED, f"workers failed: {reason}")
            return True
        if self.status == schemas.JobStatus.RUNNING and self.closed and not self.work_pending():
            self.drain_started = time.time()
            await self._set_status(schemas.JobStatus.DRAINING)
            await self.broker.broadcast_control(self.id, {"cmd": "drain"})
            await self.emit("job.draining", "all work done; asking workers to exit")
            return False
        if self.status == schemas.JobStatus.DRAINING:
            gone = bs.phase in ("complete", "missing") or bs.active == 0
            if gone or time.time() - (self.drain_started or 0) > DRAIN_TIMEOUT:
                await self._finish(
                    schemas.JobStatus.SUCCEEDED, f"completed {self.completed} task(s), {self.failed} failed"
                )
                return True
            # workers started after the broadcast (e.g. a replaced crash) also need the signal
            await self.broker.broadcast_control(self.id, {"cmd": "drain"})
        return False

    async def _heal_orphans(self, m: dict[str, Any]) -> None:
        """Dispatches again outstanding tasks that no live worker is processing.

        Such orphans are neither queued nor in progress, e.g. a result lost while the engine
        was down. They are re-sent after ORPHAN_GRACE of suspicious quiet.

        Args:
            m: The metrics just collected.
        """
        suspect = (
            self.status in (schemas.JobStatus.RUNNING, schemas.JobStatus.DRAINING)
            and m["in_flight"] > 0
            and m["queue_depth"] == 0
            and m["live_workers"] > 0
            and m["busy_workers"] == 0
        )
        now = time.time()
        if not suspect:
            self._orphans_since = None
            return
        if self._orphans_since is None:
            self._orphans_since = now
            return
        if now - self._orphans_since < max(ORPHAN_GRACE, 4 * self.hb_interval):
            return
        self._orphans_since = None
        entries = list(self.tasks.items())
        for tid, st in entries:
            st.attempt += 1
            await self.store.update(db.TaskRow, tid, attempts=st.attempt)
        await self.broker.publish_tasks(self.id, [self._message(tid, st) for tid, st in entries])
        await self.emit(
            "task.redispatched",
            f"{len(entries)} orphaned task(s) dispatched again",
            tasks=[t for t, _ in entries][:50],
        )

    # ---- Analyse: facts
    def job_fact(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "pattern": self.pattern,
            "status": self.status.value,
            "workers": self.workers,
            "active": self.backend_status.active,
            "ready": self.backend_status.ready,
            "min_workers": self.spec.min_workers,
            "max_workers": self.spec.max_workers,
            "group_id": self.group_id,
            "budget_free": self.engine.budget_free(self),
            "closed": self.closed,
            "max_retries": self.spec.max_retries,
        }

    def facts(self, metrics: dict[str, Any]) -> dict[str, list[dict]]:
        return {"Job": [self.job_fact()], "Metrics": [metrics]}

    # ---- Plan / Execute
    async def execute(self, firings: list[dsl.Firing]) -> None:
        for f in firings:
            prom.RULES.labels(self.catalog.scope, self.id, f.rule).inc()
            if f.error:
                await self.emit("rule.error", f"rule {f.rule!r}: {f.error}", rule=f.rule)
                continue
            for call in f.calls:
                try:
                    await self.apply(call)
                except Exception as e:  # pylint: disable=broad-exception-caught  # a failing action is reported, the other actions still run
                    await self.emit(
                        "action.error", f"{call.name}() from rule {f.rule!r} failed: {e}", rule=f.rule
                    )

    async def apply(self, call: dsl.ActionCall) -> None:
        """Executes one action a rule fired (common actions here, pattern ones in subclasses).

        Args:
            call: The action and its evaluated arguments.

        Raises:
            ValueError: The action is not supported by this pattern.
            JobStateError: The action is not possible in the job's current state.
        """
        source = f"rule:{call.rule}"
        a = call.args
        if call.name == "log":
            await self.emit("rule.log", format_message(a[0], a[1:]), rule=call.rule)
        elif call.name == "scale":
            await self.set_workers(int(a[0]), source)
        elif call.name == "scale_up":
            await self.set_workers(self.workers + int(a[0] if a else 1), source)
        elif call.name == "scale_down":
            await self.set_workers(self.workers - int(a[0] if a else 1), source)
        elif call.name == "suspend":
            await self.suspend(source)
        elif call.name == "resume":
            await self.resume(source)
        elif call.name == "escalate":
            self.raise_escalation(str(a[0]), int(a[1]) if len(a) > 1 and a[1] is not None else 1, call.rule)
        else:
            await self.apply_pattern_action(call, source)

    async def apply_pattern_action(self, call: dsl.ActionCall, source: str) -> None:
        raise ValueError(f"unsupported action {call.name}")

    # ================================================================ control interface
    async def set_workers(self, requested: int, source: str) -> int:
        """Scales the job, within its bounds and its group's budget.

        Args:
            requested: The worker count asked for.
            source: Who asks (``api``, ``rule:<name>``, ``group...``), for events and metrics.

        Returns:
            The worker count granted. When the group budget limits it, the shortfall is
            escalated to the group manager.
        """
        bounded = max(self.spec.min_workers, min(self.spec.max_workers, int(requested)))
        granted = self.engine.allowance(self, bounded)
        if granted < bounded:
            self.raise_escalation("budget", bounded - granted, source, target=bounded)
        if granted == self.workers:
            return self.workers
        old = self.workers
        self.workers = granted
        if self.status in (schemas.JobStatus.RUNNING, schemas.JobStatus.DRAINING):
            await self.backend.scale(self.id, granted)
        await self.store.update(db.JobRow, self.id, workers=granted)
        prom.ACTIONS.labels(self.catalog.scope, self.id, "scale", source.split(":")[0]).inc()
        note = f" (requested {requested}, limited by group budget)" if granted < bounded else ""
        await self.emit(
            "job.scaled",
            f"workers {old} -> {granted} by {source}{note}",
            old=old,
            new=granted,
            requested=requested,
            source=source,
        )
        if granted < old and not source.startswith("group"):
            self.escalations.pop("budget", None)  # the job no longer needs what it asked for
        self._settle_budget_escalation()
        return granted

    async def suspend(self, source: str) -> None:
        if self.status != schemas.JobStatus.RUNNING:
            raise JobStateError(f"cannot suspend a {self.status.value} job")
        await self.backend.suspend(self.id)
        await self._set_status(schemas.JobStatus.SUSPENDED)
        prom.ACTIONS.labels(self.catalog.scope, self.id, "suspend", source.split(":")[0]).inc()
        await self.emit("job.suspended", f"suspended by {source}; queued work is kept", source=source)

    async def resume(self, source: str) -> None:
        if self.status != schemas.JobStatus.SUSPENDED:
            raise JobStateError(f"cannot resume a {self.status.value} job")
        self.status = schemas.JobStatus.RUNNING  # count towards the group budget again
        self.workers = self.engine.allowance(self, self.workers)
        await self.backend.resume(self.id, self.workers)
        await self._set_status(schemas.JobStatus.RUNNING)
        await self.store.update(db.JobRow, self.id, workers=self.workers)
        prom.ACTIONS.labels(self.catalog.scope, self.id, "resume", source.split(":")[0]).inc()
        await self.emit("job.resumed", f"resumed by {source} with {self.workers} worker(s)", source=source)

    async def cancel(self, source: str = "api") -> None:
        if self.status in schemas.TERMINAL:
            raise JobStateError(f"job already {self.status.value}")
        async with self._lock:
            ids = list(self.tasks)
            self.tasks.clear()
            if ids:
                await self.store.update_where(
                    db.TaskRow, {"job_id": self.id, "status": "queued"}, status="cancelled"
                )
            await self._finish(schemas.JobStatus.CANCELLED, f"cancelled by {source}")

    async def set_management(self, mode: str) -> None:
        self.management = mode
        await self.store.update(db.JobRow, self.id, management=mode)
        await self.emit("job.management", f"management switched to {mode}")

    async def set_contract(self, rulebase: dsl.RuleBase | None, info: dict) -> None:
        async with self._lock:
            self.rulebase = rulebase
            self.contract_info = info
            await self.store.update(db.JobRow, self.id, contract=info)
        n = len(rulebase.rules) if rulebase else 0
        await self.emit("job.contract", f"contract replaced ({n} rule(s))")

    # ================================================================ escalation
    def raise_escalation(self, reason: str, amount: int, source: str, target: int | None = None) -> None:
        """Asks the group manager for help.

        Args:
            reason: Why (``budget`` or a contract-defined reason); one escalation per reason.
            amount: How much is missing (e.g. workers).
            source: Who escalates, for the event.
            target: For budget escalations, the worker count the job wants; the outstanding
                amount shrinks as the group grants workers.
        """
        if reason not in self.escalations:
            self._bg(
                self.emit(
                    "job.escalated",
                    f"escalated to the group: {reason} (amount {amount}) from {source}",
                    reason=reason,
                    amount=amount,
                )
            )
        self.escalations[reason] = {
            "job_id": self.id,
            "job_name": self.name,
            "reason": reason,
            "amount": amount,
            "target": target,
            "since": self.escalations.get(reason, {}).get("since", time.time()),
            "refreshed": time.time(),
            "source": source,
        }

    def _settle_budget_escalation(self) -> None:
        e = self.escalations.get("budget")
        if e and e.get("target") is not None:
            e["amount"] = e["target"] - self.workers
            if e["amount"] <= 0 or self.workers >= self.spec.max_workers:
                del self.escalations["budget"]

    def _bg(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _expire_escalations(self) -> None:
        ttl = max(ESCALATION_TTL, 3 * self.interval)
        now = time.time()
        for r in [r for r, e in self.escalations.items() if now - e["refreshed"] > ttl]:
            del self.escalations[r]

    # ================================================================ views
    def counts(self) -> dict[str, int]:
        return {
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "retried": self.retried,
            "outstanding": len(self.tasks),
        }


async def cancel_and_wait(task: asyncio.Task | None) -> None:
    """Cancels ``task`` (if running) and waits until it has finished."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # pylint: disable=broad-exception-caught  # waiting for a cancelled task: its outcome is irrelevant
        pass


class JobStateError(errors.ConflictError):
    """A job or group cannot do this in its current state."""


def format_message(fmt: Any, args: list[Any]) -> str:
    """Formats a ``log()`` action message: each ``{}`` takes the next argument (null for None)."""
    text = str(fmt)
    for a in args:
        if "{}" not in text:
            text += f" {a}"
        else:
            text = text.replace("{}", "null" if a is None else str(a), 1)
    return text
