"""Group manager: the manager one level up.

A group shares a worker budget among member jobs. Members escalate when they
cannot meet their contract locally (e.g. scaling clipped by the budget); the
group contract decides how to rebalance.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TYPE_CHECKING

from aje.application import schemas
from aje.application.runtime import job as job_lib
from aje.application.runtime import prom
from aje.domain import dsl
from aje.infrastructure.persistence import db

if TYPE_CHECKING:
    from aje.application.runtime import engine as engine_lib

log = logging.getLogger("aje.group")

COUNTED = (schemas.JobStatus.PENDING, schemas.JobStatus.RUNNING, schemas.JobStatus.DRAINING)


class GroupManager:
    """Second management level: shares a worker budget among member jobs.

    Members ask for workers through ``allowance``; the group never grants more than its
    budget. When management is active, the group contract runs each ``interval`` over
    the members' statistics and escalations.
    """

    catalog = dsl.GROUP

    def __init__(self, engine: engine_lib.AutonomicEngine, row: db.GroupRow, rulebase: dsl.RuleBase | None):
        self.engine = engine
        self.id = row.id
        self.name = row.name
        self.status = row.status
        self.management = row.management
        self.max_workers = row.max_workers
        self.contract_info = row.contract or {}
        self.created_at = row.created_at
        self.rulebase = rulebase
        self.interval = engine.settings.group_interval
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def members(self) -> list[job_lib.JobManager]:
        return [m for m in self.engine.managers.values() if m.group_id == self.id]

    def used_workers(self, exclude: str | None = None) -> int:
        return sum(m.workers for m in self.members() if m.id != exclude and m.status in COUNTED)

    def allowance(self, job: job_lib.JobManager, requested: int) -> int:
        if self.max_workers is None or requested <= job.workers:
            return requested
        free = self.max_workers - self.used_workers(exclude=job.id)
        return max(min(job.workers, requested), min(requested, free))

    def free(self) -> int | None:
        return None if self.max_workers is None else self.max_workers - self.used_workers()

    async def emit(self, kind: str, message: str, **data) -> None:
        await self.engine.bus.emit(kind, message, group_id=self.id, data=data or None)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name=f"aje-group-{self.id}")

    async def stop(self) -> None:
        await job_lib.cancel_and_wait(self._task)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # pylint: disable=broad-exception-caught  # a failing tick must not kill the group manager
                log.exception("group tick failed for %s", self.id)
            await asyncio.sleep(self.interval)

    def facts(self) -> dict[str, list[dict[str, Any]]]:
        """Working memory for the group contract: Group, JobStat per live member, Escalation."""
        now = time.time()
        members = self.members()
        stats, escalations = [], []
        for m in members:
            if m.status in schemas.TERMINAL:
                continue
            lm = m.last_metrics or {}
            stats.append(
                {
                    "id": m.id,
                    "name": m.name,
                    "pattern": m.pattern,
                    "status": m.status.value,
                    "workers": m.workers,
                    "min_workers": m.spec.min_workers,
                    "max_workers": m.spec.max_workers,
                    "queue_depth": lm.get("queue_depth", 0),
                    "outstanding": lm.get("outstanding", 0),
                    "throughput": lm.get("throughput", 0.0),
                    "avg_latency": lm.get("avg_latency"),
                    "failure_rate": lm.get("failure_rate", 0.0),
                    "idle_seconds": lm.get("idle_seconds", 0.0),
                    "avg_utilisation": lm.get("avg_utilisation", 0.0),
                }
            )
            for e in m.escalations.values():
                escalations.append(
                    {
                        "job_id": e["job_id"],
                        "job_name": e["job_name"],
                        "reason": e["reason"],
                        "amount": e["amount"],
                        "age": round(now - e["since"], 2),
                    }
                )
        group = {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "max_workers": self.max_workers,
            "used_workers": self.used_workers(),
            "free": self.free(),
            "jobs": len(members),
            "active_jobs": len(stats),
        }
        return {"Group": [group], "JobStat": stats, "Escalation": escalations}

    async def tick(self) -> None:
        """Marks the group finished when every member is, then runs the group contract."""
        async with self._lock:
            members = self.members()
            if members and all(m.status in schemas.TERMINAL for m in members) and self.status == "running":
                await self.set_status("finished")
            if self.management != "active" or self.rulebase is None or self.status != "running":
                return
            firings = self.rulebase.run(self.facts())
            for err in set(self.rulebase.eval_errors):
                await self.emit("rule.error", err)
            for f in firings:
                prom.RULES.labels("group", self.id, f.rule).inc()
                if f.error:
                    await self.emit("rule.error", f"rule {f.rule!r}: {f.error}", rule=f.rule)
                    continue
                for call in f.calls:
                    try:
                        await self.apply(call)
                    except Exception as e:  # pylint: disable=broad-exception-caught  # a failing action is reported, the other actions still run
                        await self.emit("action.error", f"{call.name}() from rule {f.rule!r} failed: {e}")

    def _member(self, job_id: Any) -> job_lib.JobManager:
        m = self.engine.managers.get(str(job_id))
        if m is None or m.group_id != self.id:
            raise ValueError(f"job {job_id} is not an active member of group {self.name}")
        return m

    async def apply(self, call: dsl.ActionCall) -> None:
        """Executes one action fired by the group contract.

        Args:
            call: The action and its evaluated arguments.

        Raises:
            ValueError: The action names a job that is not a live member.
        """
        source = f"group-rule:{call.rule}"
        a = call.args
        if call.name == "log":
            await self.emit("rule.log", job_lib.format_message(a[0], a[1:]), rule=call.rule)
        elif call.name == "scale_job":
            await self._member(a[0]).set_workers(int(a[1]), source)
        elif call.name == "suspend_job":
            await self._member(a[0]).suspend(source)
        elif call.name == "resume_job":
            await self._member(a[0]).resume(source)
        elif call.name == "set_budget":
            await self.set_budget(int(a[0]), source)
        elif call.name == "dismiss":
            self._member(a[0]).escalations.clear()
        prom.ACTIONS.labels("group", self.id, call.name, "rule").inc()

    # ---------------------------------------------------------------- control interface
    async def set_status(self, status: str) -> None:
        """Records and announces a new group status."""
        self.status = status
        await self.engine.store.update(db.GroupRow, self.id, status=status)
        await self.emit(f"group.{status}", f"group {status}")

    async def set_budget(self, max_workers: int | None, source: str = "api") -> None:
        """Changes the worker budget; members that no longer fit shrink, largest first.

        Args:
            max_workers: The new budget; None removes the limit.
            source: Who changes it, for the event.
        """
        old, self.max_workers = self.max_workers, max_workers
        await self.engine.store.update(db.GroupRow, self.id, max_workers=max_workers)
        await self.emit("group.budget", f"budget {old} -> {max_workers} by {source}")
        if max_workers is not None:  # shrink members that no longer fit, largest first
            over = self.used_workers() - max_workers
            for m in sorted((m for m in self.members() if m.status in COUNTED), key=lambda m: -m.workers):
                if over <= 0:
                    break
                cut = min(over, m.workers - m.spec.min_workers)
                if cut > 0:
                    await m.set_workers(m.workers - cut, f"{source}:budget")
                    over -= cut

    async def for_members(self, op: str, source: str = "api") -> dict[str, str]:
        """Suspends, resumes or cancels every member, then sets the group status to match.

        Args:
            op: ``suspend``, ``resume`` or ``cancel``.
            source: Who asks, for the members' events.

        Returns:
            Job id -> ``ok``, ``skipped (<status>)`` or the error message.
        """
        out = {}
        for m in self.members():
            try:
                if op == "suspend" and m.status == schemas.JobStatus.RUNNING:
                    await m.suspend(source)
                elif op == "resume" and m.status == schemas.JobStatus.SUSPENDED:
                    await m.resume(source)
                elif op == "cancel" and m.status not in schemas.TERMINAL:
                    await m.cancel(source)
                else:
                    out[m.id] = f"skipped ({m.status.value})"
                    continue
                out[m.id] = "ok"
            except job_lib.JobStateError as e:
                out[m.id] = str(e)
        if op == "cancel":
            await self.set_status("cancelled")
        elif op == "suspend":
            await self.set_status("suspended")
        elif op == "resume":
            await self.set_status("running")
        return out

    async def set_management(self, mode: str) -> None:
        self.management = mode
        await self.engine.store.update(db.GroupRow, self.id, management=mode)
        await self.emit("group.management", f"management switched to {mode}")

    async def set_contract(self, rulebase: dsl.RuleBase | None, info: dict) -> None:
        async with self._lock:
            self.rulebase = rulebase
            self.contract_info = info
            await self.engine.store.update(db.GroupRow, self.id, contract=info)
        n = len(rulebase.rules) if rulebase else 0
        await self.emit("group.contract", f"contract replaced ({n} rule(s))")
