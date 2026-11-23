"""Fact and action catalogs: what a contract may observe and what it may change.

Each management scope (farm job, data-parallel job, group) publishes a
catalog. Contracts are validated against it at submission time, so a rule can
only call actions that map to a *non-functional knob* of the pattern. That is
the guarantee that management never alters what a job computes.
"""

from __future__ import annotations

import pydantic


class ActionSpec(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    name: str
    min_args: int
    max_args: int | None
    knob: str | None
    doc: str


class Catalog(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    scope: str
    facts: dict[str, dict[str, str]]
    actions: dict[str, ActionSpec]
    knobs: dict[str, str] = pydantic.Field(default_factory=dict)

    def describe(self) -> dict:
        return {
            "scope": self.scope,
            "knobs": self.knobs,
            "facts": self.facts,
            "actions": {
                a.name: {
                    "args": [a.min_args, a.max_args],
                    "knob": a.knob,
                    "doc": a.doc,
                }
                for a in self.actions.values()
            },
        }


LOG = ActionSpec(
    name="log",
    min_args=1,
    max_args=None,
    knob=None,
    doc='log("text with {} placeholders", args...) - record an event',
)

_JOB_FACT = {
    "id": "job id",
    "name": "job name",
    "pattern": "'farm' or 'data_parallel'",
    "status": "pending|running|suspended|draining|succeeded|failed|cancelled",
    "workers": "desired number of workers (the parallelism knob)",
    "active": "workers currently running according to the cluster",
    "ready": "workers ready according to the cluster",
    "min_workers": "lower bound for workers",
    "max_workers": "upper bound for workers",
    "group_id": "owning group id or null",
    "budget_free": "workers still available in the group budget, or null",
    "closed": "true once no more input will be accepted",
    "max_retries": "retries per task before it is marked failed",
}

_METRICS_FACT = {
    "queue_depth": "messages waiting in the task queue",
    "consumers": "workers consuming from the task queue",
    "in_flight": "tasks being processed right now",
    "outstanding": "tasks dispatched and not yet finished (queued + in flight)",
    "submitted": "tasks submitted since start",
    "completed": "tasks completed successfully since start",
    "failed": "tasks failed permanently since start",
    "retried": "task attempts retried since start",
    "throughput": "successful tasks per minute over the window",
    "arrival_rate": "tasks submitted per minute over the window",
    "avg_latency": "mean seconds from enqueue to result over the window (null if none)",
    "p95_latency": "95th percentile latency over the window (null if none)",
    "avg_service_time": "mean seconds a worker spends on a task (null if none)",
    "failure_rate": "failed attempts / attempts over the window (0..1)",
    "live_workers": "workers that sent a heartbeat recently",
    "busy_workers": "live workers currently processing",
    "utilisation": "busy_workers / live_workers right now (0..1)",
    "avg_utilisation": "mean utilisation over the window (0..1)",
    "idle_seconds": "seconds since the last task arrival or completion",
    "elapsed": "seconds since the job started",
    "pod_failures": "worker processes/pods that failed",
}

_DP_METRICS = {
    "partitions": "partitions used to split the next batch",
    "pending_batches": "batches submitted and not yet gathered",
    "pending_partitions": "partitions not yet completed across pending batches",
    "batches_done": "batches gathered successfully",
    "batches_failed": "batches failed",
    "last_batch_time": "seconds the last batch took from scatter to gather (null if none)",
    "partition_skew": "slowest / fastest partition service time in the last batch (null if none)",
}

_LIFECYCLE = {
    "suspend": ActionSpec(
        name="suspend",
        min_args=0,
        max_args=0,
        knob="lifecycle",
        doc="suspend(): stop all workers; queued tasks are kept",
    ),
    "resume": ActionSpec(
        name="resume",
        min_args=0,
        max_args=0,
        knob="lifecycle",
        doc="resume(): restart workers after a suspension",
    ),
}

_SCALING = {
    "scale": ActionSpec(
        name="scale",
        min_args=1,
        max_args=1,
        knob="workers",
        doc="scale(n): set the number of workers (clamped to bounds and group budget)",
    ),
    "scale_up": ActionSpec(
        name="scale_up", min_args=0, max_args=1, knob="workers", doc="scale_up(k=1): add k workers"
    ),
    "scale_down": ActionSpec(
        name="scale_down", min_args=0, max_args=1, knob="workers", doc="scale_down(k=1): remove k workers"
    ),
    "escalate": ActionSpec(
        name="escalate",
        min_args=1,
        max_args=2,
        knob=None,
        doc='escalate("reason", amount=1): ask the group manager for help',
    ),
}

FARM = Catalog(
    scope="farm",
    facts={"Job": _JOB_FACT, "Metrics": _METRICS_FACT},
    actions={**_SCALING, **_LIFECYCLE, "log": LOG},
    knobs={
        "workers": "number of identical stateless workers pulling from the task queue",
        "lifecycle": "suspend / resume",
    },
)

DATA_PARALLEL = Catalog(
    scope="data_parallel",
    facts={
        "Job": {**_JOB_FACT, "partitions": "partitions used to split the next batch"},
        "Metrics": {**_METRICS_FACT, **_DP_METRICS},
    },
    actions={
        **_SCALING,
        **_LIFECYCLE,
        "log": LOG,
        "set_partitions": ActionSpec(
            name="set_partitions",
            min_args=1,
            max_args=1,
            knob="partitions",
            doc="set_partitions(n): split the next batches into n partitions (applied at batch boundaries)",
        ),
    },
    knobs={
        "workers": "number of identical stateless workers pulling partitions",
        "partitions": "how many slices each batch is cut into (applied between batches)",
        "lifecycle": "suspend / resume",
    },
)

GROUP = Catalog(
    scope="group",
    facts={
        "Group": {
            "id": "group id",
            "name": "group name",
            "status": "running|suspended|cancelled|finished",
            "max_workers": "worker budget for the whole group (null = unlimited)",
            "used_workers": "sum of desired workers of active member jobs",
            "free": "max_workers - used_workers (null if unlimited)",
            "jobs": "number of member jobs",
            "active_jobs": "member jobs still running or draining",
        },
        "JobStat": {
            "id": "member job id",
            "name": "member job name",
            "pattern": "farm | data_parallel",
            "status": "member job status",
            "workers": "desired workers",
            "min_workers": "lower bound",
            "max_workers": "upper bound",
            "queue_depth": "queued tasks",
            "outstanding": "queued + in-flight tasks",
            "throughput": "tasks per minute",
            "avg_latency": "mean latency seconds (null if none)",
            "failure_rate": "failure rate 0..1",
            "idle_seconds": "seconds since last activity",
            "avg_utilisation": "mean utilisation over the window",
        },
        "Escalation": {
            "job_id": "job that escalated",
            "job_name": "its name",
            "reason": "'budget' (scaling clipped by group budget) or a reason given in escalate()",
            "amount": "workers (or units) requested",
            "age": "seconds since the escalation was raised",
        },
    },
    actions={
        "scale_job": ActionSpec(
            name="scale_job",
            min_args=2,
            max_args=2,
            knob="member.workers",
            doc="scale_job(job_id, n): set workers of a member job",
        ),
        "suspend_job": ActionSpec(
            name="suspend_job", min_args=1, max_args=1, knob="member.lifecycle", doc="suspend_job(job_id)"
        ),
        "resume_job": ActionSpec(
            name="resume_job", min_args=1, max_args=1, knob="member.lifecycle", doc="resume_job(job_id)"
        ),
        "set_budget": ActionSpec(
            name="set_budget",
            min_args=1,
            max_args=1,
            knob="budget",
            doc="set_budget(n): change the group worker budget",
        ),
        "dismiss": ActionSpec(
            name="dismiss",
            min_args=1,
            max_args=1,
            knob=None,
            doc="dismiss(job_id): drop pending escalations of a job",
        ),
        "log": LOG,
    },
    knobs={
        "member.workers": "workers of each member job",
        "member.lifecycle": "suspend / resume of member jobs",
        "budget": "total worker budget",
    },
)

CATALOGS = {c.scope: c for c in (FARM, DATA_PARALLEL, GROUP)}
