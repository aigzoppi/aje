"""Public API schemas."""

from __future__ import annotations

import datetime
import enum
from typing import Any, Literal

import pydantic

from aje.application.ports import backend
from aje.domain import cron as cron_lib
from aje.domain import dsl
from aje_worker import context
from aje_worker import protocol


class JobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"
    DRAINING = "draining"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}

Pattern = Literal["farm", "data_parallel"]
Management = Literal["active", "passive"]
Gather = Literal["concat", "list", "sum", "merge"]


class Resources(pydantic.BaseModel):
    cpu_request: str | None = None
    cpu_limit: str | None = None
    memory_request: str | None = None
    memory_limit: str | None = None


class WorkerTemplate(pydantic.BaseModel):
    """How to run one worker. On Kubernetes this becomes the Job pod template."""

    image: str = "aje-worker:latest"
    command: list[str] = pydantic.Field(
        ..., min_length=1, examples=[["python", "-m", "aje_worker", "aje_worker.examples:square"]]
    )
    args: list[str] = []
    env: dict[str, str] = {}
    resources: Resources | None = None
    node_selector: dict[str, str] = {}
    service_account: str | None = None
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] | None = None
    backoff_limit: int = pydantic.Field(20, ge=0, description="worker crashes tolerated before the job fails")


class ContractSpec(pydantic.BaseModel):
    """Management contract: rule source in the AJE rule language, or a built-in one."""

    source: str | None = pydantic.Field(None, description="contract text (package/global/rule ... end)")
    builtin: str | None = pydantic.Field(
        None, description="name of a built-in contract, e.g. farm-throughput"
    )
    globals: dict[str, Any] = pydantic.Field(
        {}, description="override values of the contract's global parameters"
    )

    @pydantic.model_validator(mode="after")
    def _one_source(self):
        if self.source and self.builtin:
            raise ValueError("give either 'source' or 'builtin', not both")
        return self


class JobTemplate(pydantic.BaseModel):
    """Everything needed to launch a job except its name (used by schedules)."""

    pattern: Pattern
    worker: WorkerTemplate
    workers: int = pydantic.Field(1, ge=0, description="initial workers (farm default: 1)")
    min_workers: int = pydantic.Field(1, ge=0)
    max_workers: int = pydantic.Field(10, ge=1)
    management: Management = "active"
    contract: ContractSpec | None = pydantic.Field(
        None, description="omit to use the pattern's default built-in contract"
    )
    max_retries: int = pydantic.Field(3, ge=0)
    monitor_interval: float | None = pydantic.Field(None, gt=0)
    monitor_window: float | None = pydantic.Field(None, gt=0)
    labels: dict[str, str] = {}
    output_queue: str | None = pydantic.Field(
        None,
        pattern=protocol.OUTPUT_ID_PATTERN,
        description="id of the output stream: the RabbitMQ queue aje.output.<id> that receives the job's "
        "results as msgpack messages carrying the job_id. Jobs (e.g. every run of a schedule) may share "
        "one. Omit to get a new unique id.",
    )
    jobcontext: context.JobContext = pydantic.Field(
        default_factory=context.JobContext,
        description="configuration every worker receives (ctx.job_context): any parameters, plus an optional "
        "'datastore' (PostgreSQL input/output tables). Never put credentials here: it is stored "
        "and returned by the API; point to them with datastore.dsn_env / dsn_secret.",
    )

    # farm
    tasks: list[Any] = pydantic.Field([], description="farm: initial tasks (one payload each)")
    close_when_done: bool = pydantic.Field(False, description="farm: finish once the initial tasks are done")

    # data parallel
    partitions: int = pydantic.Field(4, ge=1, description="data_parallel: initial partitions per batch")
    gather: Gather = pydantic.Field(
        "concat", description="data_parallel: how partition results are assembled"
    )
    items: list[Any] | None = pydantic.Field(None, description="data_parallel: optional first batch")

    @pydantic.model_validator(mode="after")
    def _check(self):
        if self.min_workers > self.max_workers:
            raise ValueError("min_workers must be <= max_workers")
        if not self.min_workers <= self.workers <= self.max_workers:
            raise ValueError("workers must be between min_workers and max_workers")
        if self.pattern == "farm" and self.items is not None:
            raise ValueError("'items' is for data_parallel jobs; use 'tasks' for a farm")
        if self.pattern == "data_parallel" and self.tasks:
            raise ValueError("'tasks' is for farm jobs; use 'items' (a batch) for data_parallel")
        self.jobcontext.check()
        return self


class JobCreate(JobTemplate):
    """A job submission: a template plus its name."""

    model_config = pydantic.ConfigDict(
        json_schema_extra={
            "example": {
                "name": "squares",
                "pattern": "farm",
                "worker": {"command": ["python", "-m", "aje_worker", "aje_worker.examples:square"]},
                "max_workers": 8,
                "contract": {"builtin": "farm-throughput", "globals": {"backlog_per_worker": 3}},
                "tasks": [1, 2, 3, 4, 5],
                "close_when_done": True,
            }
        }
    )

    name: str = pydantic.Field(
        ...,
        pattern=r"^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$",
        description="DNS-1123 label, at most 40 characters",
    )


class GroupCreate(pydantic.BaseModel):
    name: str = pydantic.Field(..., pattern=r"^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$")
    max_workers: int | None = pydantic.Field(None, ge=1, description="worker budget shared by the members")
    management: Management = "active"
    contract: ContractSpec | None = None
    jobs: list[JobCreate] = []


class TasksIn(pydantic.BaseModel):
    tasks: list[Any] = pydantic.Field(..., min_length=1)
    close: bool = pydantic.Field(False, description="no more tasks after these")


class BatchIn(pydantic.BaseModel):
    items: list[Any] = pydantic.Field(..., min_length=1)
    partitions: int | None = pydantic.Field(None, ge=1, description="override the managed partition count")


class ScaleIn(pydantic.BaseModel):
    workers: int = pydantic.Field(..., ge=0)


class ManagementIn(pydantic.BaseModel):
    management: Management


class ContractValidateIn(pydantic.BaseModel):
    scope: Literal["farm", "data_parallel", "group"]
    contract: ContractSpec


class BudgetIn(pydantic.BaseModel):
    max_workers: int | None = pydantic.Field(None, ge=1)


# ---------------------------------------------------------------- outputs
# ---------------------------------------------------------------- metrics
_DOC = dsl.DATA_PARALLEL.facts["Metrics"]  # the contract catalog documents every metric


class JobMetrics(pydantic.BaseModel):
    """The ``Metrics`` fact of a job at the last management tick (what its contract sees).

    Data-parallel jobs also fill the batch fields; they are null for farms.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    ts: float = pydantic.Field(description="when the metrics were taken (epoch seconds)")
    queue_depth: int = pydantic.Field(description=_DOC["queue_depth"])
    consumers: int = pydantic.Field(description=_DOC["consumers"])
    in_flight: int = pydantic.Field(description=_DOC["in_flight"])
    outstanding: int = pydantic.Field(description=_DOC["outstanding"])
    submitted: int = pydantic.Field(description=_DOC["submitted"])
    completed: int = pydantic.Field(description=_DOC["completed"])
    failed: int = pydantic.Field(description=_DOC["failed"])
    retried: int = pydantic.Field(description=_DOC["retried"])
    throughput: float = pydantic.Field(description=_DOC["throughput"])
    arrival_rate: float = pydantic.Field(description=_DOC["arrival_rate"])
    avg_latency: float | None = pydantic.Field(description=_DOC["avg_latency"])
    p95_latency: float | None = pydantic.Field(description=_DOC["p95_latency"])
    avg_service_time: float | None = pydantic.Field(description=_DOC["avg_service_time"])
    failure_rate: float = pydantic.Field(description=_DOC["failure_rate"])
    live_workers: int = pydantic.Field(description=_DOC["live_workers"])
    busy_workers: int = pydantic.Field(description=_DOC["busy_workers"])
    utilisation: float = pydantic.Field(description=_DOC["utilisation"])
    avg_utilisation: float = pydantic.Field(description=_DOC["avg_utilisation"])
    idle_seconds: float = pydantic.Field(description=_DOC["idle_seconds"])
    elapsed: float = pydantic.Field(description=_DOC["elapsed"])
    pod_failures: int = pydantic.Field(description=_DOC["pod_failures"])
    # data_parallel only
    partitions: int | None = pydantic.Field(None, description=_DOC["partitions"])
    pending_batches: int | None = pydantic.Field(None, description=_DOC["pending_batches"])
    pending_partitions: int | None = pydantic.Field(None, description=_DOC["pending_partitions"])
    batches_done: int | None = pydantic.Field(None, description=_DOC["batches_done"])
    batches_failed: int | None = pydantic.Field(None, description=_DOC["batches_failed"])
    last_batch_time: float | None = pydantic.Field(None, description=_DOC["last_batch_time"])
    partition_skew: float | None = pydantic.Field(None, description=_DOC["partition_skew"])


class MetricsPoint(pydantic.BaseModel):
    """One point of a job's metrics history (one per management tick, the last 720 kept)."""

    ts: float = pydantic.Field(description="epoch seconds")
    queue_depth: int = pydantic.Field(description=_DOC["queue_depth"])
    outstanding: int = pydantic.Field(description=_DOC["outstanding"])
    throughput: float = pydantic.Field(description=_DOC["throughput"])
    avg_latency: float | None = pydantic.Field(description=_DOC["avg_latency"])
    avg_utilisation: float = pydantic.Field(description=_DOC["avg_utilisation"])
    live_workers: int = pydantic.Field(description=_DOC["live_workers"])
    workers: int = pydantic.Field(description="workers the job asked for at that time")


class Escalation(pydantic.BaseModel):
    """An open request of a job to its group manager."""

    job_id: str
    job_name: str
    reason: str = pydantic.Field(description="``budget`` or a reason set by the job's contract")
    amount: int = pydantic.Field(description="how much is missing, e.g. workers")
    target: int | None = pydantic.Field(description="budget escalations: the worker count the job wants")
    since: float = pydantic.Field(description="raised at (epoch seconds)")
    refreshed: float = pydantic.Field(description="last confirmed at (epoch seconds)")
    source: str = pydantic.Field(description="who raised it")


class JobMetricsOut(pydantic.BaseModel):
    """``GET /v1/jobs/{id}/metrics``: current metrics, recent history, backend and escalations."""

    job_id: str
    workers: int
    backend: backend.BackendStatus
    escalations: list[Escalation]
    current: JobMetrics | None = pydantic.Field(description="null until the first management tick")
    history: list[MetricsPoint]


class JobOut(pydantic.BaseModel):
    """A job as the API returns it: live state when managed, stored state otherwise."""

    id: str
    name: str
    pattern: str
    status: str
    group_id: str | None
    management: str
    workers: int
    min_workers: int
    max_workers: int
    partitions: int | None
    closed: bool
    message: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    finished_at: datetime.datetime | None
    contract: dict[str, Any]
    counts: dict[str, int]
    metrics: JobMetrics | None = None
    jobcontext: dict[str, Any] = {}
    output_queue: str | None = None


class GroupOut(pydantic.BaseModel):
    id: str
    name: str
    status: str
    management: str
    max_workers: int | None
    used_workers: int
    created_at: datetime.datetime
    jobs: list[JobOut]
    contract: dict[str, Any]


class EventOut(pydantic.BaseModel):
    id: int
    ts: datetime.datetime
    job_id: str | None
    group_id: str | None
    kind: str
    message: str
    data: dict[str, Any] | None


class TaskOut(pydantic.BaseModel):
    """One task of a job and its outcome."""

    id: str
    job_id: str
    status: str
    attempts: int
    batch_id: str | None
    partition: int | None
    payload: Any = None
    result: Any = None
    error: str | None
    worker: str | None
    submitted_at: datetime.datetime
    finished_at: datetime.datetime | None


class BatchOut(pydantic.BaseModel):
    id: str
    job_id: str
    status: str
    size: int
    partitions: int
    completed_partitions: int
    result: Any = None
    error: str | None
    created_at: datetime.datetime
    finished_at: datetime.datetime | None
    duration: float | None


class PartitionsIn(pydantic.BaseModel):
    partitions: int = pydantic.Field(..., ge=1, le=1024)


# ---------------------------------------------------------------- schedules
ConcurrencyPolicy = Literal["allow", "forbid", "replace"]

SCHEDULE_NAME = r"^[a-z0-9]([-a-z0-9]{0,29}[a-z0-9])?$"


class _ScheduleFields(pydantic.BaseModel):
    @pydantic.field_validator("cron", check_fields=False)
    @classmethod
    def _cron(cls, v: str | None) -> str | None:
        return None if v is None else cron_lib.check_cron(v)

    @pydantic.field_validator("timezone", check_fields=False)
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return None if v is None else cron_lib.check_timezone(v)


class ScheduleCreate(_ScheduleFields):
    """Launch a job from a template every time a cron expression fires."""

    model_config = pydantic.ConfigDict(
        json_schema_extra={
            "example": {
                "name": "nightly-squares",
                "cron": "0 2 * * *",
                "timezone": "Europe/Rome",
                "concurrency_policy": "forbid",
                "job": {
                    "pattern": "farm",
                    "worker": {"command": ["python", "-m", "aje_worker", "aje_worker.examples:square"]},
                    "tasks": [1, 2, 3, 4, 5],
                    "close_when_done": True,
                },
            }
        }
    )

    name: str = pydantic.Field(
        ...,
        pattern=SCHEDULE_NAME,
        description="DNS-1123 label, at most 31 characters; runs are named <name>-<unix minutes>",
    )
    cron: str = pydantic.Field(
        ..., description="5-field cron expression or @hourly/@daily/@weekly/@monthly/@yearly"
    )
    timezone: str = pydantic.Field("UTC", description="IANA timezone the cron expression is evaluated in")
    concurrency_policy: ConcurrencyPolicy = pydantic.Field(
        "forbid",
        description="when the previous run is still active: allow a new one, forbid (skip), "
        "or replace (cancel the previous run)",
    )
    starting_deadline_seconds: int | None = pydantic.Field(
        None,
        ge=1,
        description="a run missed while the engine was down is launched only if at most this late "
        "(null = always launch one catch-up run)",
    )
    suspended: bool = pydantic.Field(False, description="create the schedule without firing it")
    group_id: str | None = pydantic.Field(None, description="launch every run inside this group")
    job: JobTemplate


class ScheduleUpdate(_ScheduleFields):
    """Partial update: only the given fields change."""

    cron: str | None = None
    timezone: str | None = None
    concurrency_policy: ConcurrencyPolicy | None = None
    starting_deadline_seconds: int | None = pydantic.Field(None, ge=1)
    group_id: str | None = None
    job: JobTemplate | None = None


class ScheduleOut(pydantic.BaseModel):
    """A cron schedule with its timing and its active runs."""

    id: str
    name: str
    cron: str
    timezone: str
    status: str
    concurrency_policy: str
    starting_deadline_seconds: int | None
    group_id: str | None
    job: dict[str, Any]
    next_run_at: datetime.datetime | None
    last_run_at: datetime.datetime | None
    last_job_id: str | None
    active_jobs: list[str]
    created_at: datetime.datetime
    updated_at: datetime.datetime


class ScheduleRunOut(pydantic.BaseModel):
    id: int
    schedule_id: str
    scheduled_at: datetime.datetime
    created_at: datetime.datetime
    trigger: str
    status: str
    job_id: str | None
    job_status: str | None
    message: str | None
