# Autonomic Job Engine (AJE)

A backend that **submits, controls and monitors self-managing jobs and groups of jobs on Kubernetes**.

You don't hand-tune parallelism. Each job is an instance of a **parallel pattern**
(`farm` or `data_parallel`), comes with a **contract** written in a Drools-like rule
language, and gets its own **manager**. The manager watches the job and keeps adapting
it to the contract. Groups add a second management level: member jobs share a worker
budget, and members escalate to the group manager when they can't meet their contract alone.

```
                    REST / SSE / Prometheus
 client ──────────────▶  AJE API (FastAPI)
                           │
                           ▼
              ┌─────────── Engine ────────────┐
              │  GroupManager  (budget, escalations, group contract)
              │     ▲ escalate          │ scale_job / suspend_job
              │     │                   ▼
              │  JobManager ×N  Monitor → Analyse → Plan → Execute
              │     │   facts: Job, Metrics      rules: contract (.drl)
              │     │   control interface: scale · partitions · suspend · resume
              └─────┼─────────────────────────────┘
        tasks/results│(RabbitMQ classic queues)   │ Jobs (batch/v1) or local processes
                     ▼                             ▼
                  worker  worker  worker  …   (aje_worker SDK, stateless)
```

---

## 1. Concepts

| Concept | What it is |
|---|---|
| **Pattern** | A reusable way to organise parallel work, with a fixed dispatch/collect structure and a known set of *non-functional knobs*. Built in: `farm`, `data_parallel`. |
| **Worker** | Your code: a stateless handler `f(payload) -> result` in a container. It is the only functional part of a job. |
| **Dispatcher / Collector** | How work goes out and results come back: a RabbitMQ task queue (one consumer per worker, prefetch 1, manual ack) and a result queue. |
| **Control interface** | The operations that change *how* a job runs and never *what* it computes: `scale`, `set_partitions`, `suspend`, `resume`. Available through the API and to the manager. |
| **Manager** | One per job. Runs a Monitor → Analyse → Plan → Execute loop every `monitor_interval` seconds. Builds facts, evaluates the contract and executes the resulting actions on the control interface. |
| **Contract** | Rules in the AJE rule language (`.drl`-style): `when` conditions on facts, `then` actions. Parameters are `global`s you can override per job. Every pattern has a built-in default contract. |
| **Management mode** | `active`: the contract drives the job. `passive`: only the control interface is exposed, and the API (or an external controller) drives it. You can switch at runtime. |
| **Group** | A set of jobs with a shared worker budget and its own manager and contract. Members raise **escalations**; the group contract reallocates workers between them. |

### The guarantee: management never changes results

Contracts are type-checked against a **catalog** for their scope. A rule can only call
actions that map to a non-functional knob of the pattern. A farm contract that calls
`set_partitions()`, or any contract that tries to touch the image, command, payloads or
gather mode, is rejected when it is submitted (HTTP 422, with line number). Workers are
stateless and pull work from a queue, so changing the number of workers or the partitioning
of the *next* batch can't change what a job produces. A property-based test
(`tests/test_patterns.py::test_result_is_independent_of_management`) checks this with
random scaling and re-partitioning while jobs are running.

### Patterns

**Farm**: a stream of independent tasks. Each task is taken by whichever worker is free.
- Starts with **1 worker** by default.
- Knobs: `workers`, lifecycle.
- Input: tasks at submission and/or `POST /v1/jobs/{id}/tasks` at any time. `close` ends the stream.

**Data-parallel**: each batch of items is split into `partitions` contiguous slices that
workers process in parallel, and the partial results are gathered into one result per batch.
- Gather modes: `concat` (lists in order), `list`, `sum`, `merge` (dicts, numbers summed).
- Knobs: `workers` (any time), `partitions` (applied at the next batch, a safe point), lifecycle.
- If one partition fails permanently, its batch fails; other batches are unaffected.

---

## 2. Mapping to Kubernetes

| AJE | Kubernetes |
|---|---|
| a job's workers | one `batch/v1` **Job** in work-queue mode (`completions` unset) |
| `workers` knob | `spec.parallelism`, patched live; surplus pods get SIGTERM |
| `suspend` / `resume` | `spec.suspend` (queued tasks stay in RabbitMQ) |
| scale-down / suspension exit | the worker exits **143** → `podFailurePolicy: Ignore` (it doesn't count against `backoffLimit` and isn't a success) |
| normal end | a `drain` broadcast → workers finish and exit **0** → the Job becomes `Complete` |
| worker crash | the task is unacked, so RabbitMQ redelivers it; the Job controller replaces the pod (up to `backoff_limit`) |
| worker identity | `AJE_WORKER_ID` from the downward API (pod name) |
| retention | `ttlSecondsAfterFinished` (`AJE_K8S_TTL_AFTER_FINISHED`) |

The engine needs `jobs` (create/get/list/patch/delete), `jobs/status`, `pods` (list) and
`pods/log` in its namespace: see `deploy/k8s/rbac.yaml`.

---

## 3. Quick start (docker compose)

```bash
docker compose up --build          # migrations, then API on :8000, RabbitMQ UI on :15672 (aje/aje)
./examples/demo.sh                 # farm, control operations, data-parallel, group
open http://localhost:8000/docs    # OpenAPI UI
```

The `migrate` service applies the database migrations (`python -m aje.migrate`, plain SQL revisions applied with asyncpg) and
exits. The API starts only after it succeeds, with `AJE_DB_AUTO_CREATE=false`.

In the default compose setup, workers run as **local processes inside the API container**
(`AJE_BACKEND=local`, same semantics as the Kubernetes backend). Nothing else is needed to try it.

### Fractal Lab demo (React)

```bash
docker compose --profile demo up --build    # open http://localhost:8080
```

Renders the Mandelbrot or a Julia set with a farm job, one task per tile, and shows tiles,
workers and scaling decisions live from `GET /internal/events`. You can also drive the
workers yourself and try to beat the contract. See [examples/demo](examples/demo/README.md).

### Running workers on Kubernetes

For a local cluster, `.\setup\setup.ps1` (Windows) or `bash setup/microk8s.sh` (WSL / Ubuntu)
installs MicroK8s and an open registry on `localhost:32000`, then prints the settings to use.
See [setup/](setup/README.md).

```bash
docker compose --profile build build worker-image    # aje-worker:latest (or push your own)
kubectl apply -f deploy/k8s/rbac.yaml                 # namespace aje-jobs + Role
export AJE_WORKER_AMQP_URL=amqp://aje:aje@<rabbitmq address reachable from pods>:5672/
docker compose -f docker-compose.yml -f docker-compose.k8s.yml up --build
```

Submit jobs with `"worker": {"image": "<registry>/aje-worker:latest", ...}`. For kind or
minikube, load the image into the cluster and use `"image_pull_policy": "IfNotPresent"`.

---

## 4. API

Interactive documentation is at `/docs`. All `/v1` routes accept `X-API-Key` when `AJE_API_KEY` is set.
Building a UI? [docs/clientinterface.md](docs/clientinterface.md) describes how to submit jobs and follow
their progress; [docs/job-status-mechanism.md](docs/job-status-mechanism.md) explains how status updates flow
and lists the current limitations.

| | Method & path | Purpose |
|---|---|---|
| **submit** | `POST /v1/jobs` | create a farm or data-parallel job (optionally with initial tasks / first batch) |
| | `POST /v1/groups` | create a group with a budget, contract and member jobs |
| | `POST /v1/groups/{id}/jobs` | add a job to a group |
| | `POST /v1/jobs/{id}/tasks` | farm: append tasks (`"close": true` to end the stream) |
| | `POST /v1/jobs/{id}/batches` | data-parallel: scatter a batch |
| | `POST /v1/jobs/{id}/close` | no more input; the job succeeds when outstanding work is done |
| **control** | `POST /v1/jobs/{id}/scale` | set workers (clamped to bounds and group budget) |
| | `POST /v1/jobs/{id}/partitions` | data-parallel: partitions for the next batches |
| | `POST /v1/jobs/{id}/suspend` · `/resume` | lifecycle |
| | `PUT /v1/jobs/{id}/management` | `active` ↔ `passive` |
| | `GET/PUT /v1/jobs/{id}/contract` | read / hot-swap the contract |
| | `DELETE /v1/jobs/{id}` | cancel |
| | `POST /v1/groups/{id}/suspend\|resume\|cancel` | apply to all members |
| | `PUT /v1/groups/{id}/budget` · `/management` · `/contract` | group control |
| **monitor** | `GET /v1/jobs`, `GET /v1/jobs/{id}` | status, counters, latest metrics |
| | `GET /v1/jobs/{id}/metrics` | current Metrics fact + history + backend status + escalations |
| | `GET /v1/jobs/{id}/facts` | exactly what the contract saw at the last tick |
| | `GET /v1/jobs/{id}/events` | lifecycle and management decisions (each rule firing, scaling, escalation…) |
| | `GET /v1/jobs/{id}/stream` | Server-Sent Events: live events + periodic metrics |
| | `GET /v1/jobs/{id}/tasks[/{task}]`, `/batches[/{batch}]` | results |
| | `GET /v1/jobs/{id}/workers` · `/logs` | pods/processes and their logs |
| | `GET /v1/groups/{id}`, `/facts`, `/events`, `/stream` | group views |
| | `GET /metrics` | Prometheus |
| **schedules** | `POST /v1/schedules` | cron schedule that launches a job from a template |
| | `GET /v1/schedules`, `GET/PATCH/DELETE /v1/schedules/{id}` | list, inspect, change, delete (`?cancel_active=true` also cancels running jobs) |
| | `POST /v1/schedules/{id}/suspend\|resume\|trigger` | pause, resume, run now |
| | `GET /v1/schedules/{id}/runs` | started, skipped and failed runs with their job status |
| **contracts** | `POST /v1/contracts/validate` | parse and type-check without submitting |
| | `GET /v1/contracts/catalog/{farm\|data_parallel\|group}` | facts, fields and actions available |
| | `GET /v1/contracts/builtin` | built-in contracts with source |

### Job request

```jsonc
{
  "name": "squares",                       // DNS-1123 label
  "pattern": "farm",                       // farm | data_parallel
  "worker": {
    "image": "registry/aje-worker:1.0",    // Kubernetes only
    "command": ["python", "-m", "aje_worker", "mypkg.handlers:process"],
    "env": {"MODEL": "small"},
    "resources": {"cpu_request": "250m", "memory_limit": "512Mi"},
    "node_selector": {}, "service_account": null, "backoff_limit": 20
  },
  "workers": 1, "min_workers": 1, "max_workers": 10,
  "management": "active",                  // active | passive
  "contract": {"builtin": "farm-throughput", "globals": {"backlog_per_worker": 3}},
  //   or    {"source": "rule \"...\" when ... then ... end", "globals": {...}}
  //   omitted -> default built-in contract of the pattern
  "max_retries": 3,
  "output_queue": "nightly-results",      // output stream id (aje.output.<id>); omitted -> a new unique id
  "monitor_interval": 5, "monitor_window": 60,
  "tasks": [1, 2, 3], "close_when_done": false,            // farm
  "partitions": 4, "gather": "concat", "items": [ ... ]    // data_parallel
}
```

---

### Cron schedules

```json
POST /v1/schedules
{
  "name": "nightly-squares",
  "cron": "0 2 * * *",
  "timezone": "Europe/Rome",
  "concurrency_policy": "forbid",
  "starting_deadline_seconds": 600,
  "job": {"pattern": "farm", "worker": {"command": ["python", "-m", "aje_worker", "aje_worker.examples:square"]},
          "tasks": [1, 2, 3], "close_when_done": true}
}
```

- `job` is a job request without `name`. Each run is named `<schedule>-<unix minutes>`, like a Kubernetes CronJob, and carries the label `aje.io/schedule=<id>`. Every run is a normal autonomic job, managed by its contract.
- `cron` is 5 fields or `@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly`, evaluated in `timezone` (DST aware).
- `concurrency_policy`: `forbid` (default) skips a run while the previous one is active, `replace` cancels it, and `allow` runs both. A farm without `close_when_done` never finishes, so with `forbid` it blocks later runs.
- After a restart, a run missed while the engine was down is launched once, if it is at most `starting_deadline_seconds` late (always, when unset). Fire times that pass while a schedule is suspended are not caught up.
- `group_id` launches every run inside a group, under its budget.
- Schedules live in the engine, like managers. They are not Kubernetes CronJobs, so they work with every backend.

## 5. The contract language

Contracts look like Drools rules:

```drl
package my.contracts

global int    backlog_per_worker = 5;     // parameters, overridable per job
global double target_latency     = 30.0;

rule "grow fast on backlog"
    salience 30                           // higher fires first
    cooldown "10s"                        // not eligible again for 10 s after firing
    activation-group "scaling"            // only one rule of the group fires per tick
when
    $j : Job( status == "running", workers < max_workers )
    $m : Metrics( queue_depth > backlog_per_worker * $j.workers )
then
    scale(min($j.max_workers, $j.workers * 2));
    log("backlog {} with {} workers", $m.queue_depth, $j.workers);
end
```

**Structure**: `package` and `import` (optional, informational); `global [type] name = constant-expr;`
(types: `int`, `long`, `double`, `float`, `String`, `boolean`, or none); then `rule "name" … end`.

**Rule attributes**: `salience n`, `cooldown "500ms|30s|2m|1h"`, `activation-group "g"`,
`no-loop [true|false]` (always effectively on: facts are a snapshot per tick), `enabled false`.

**Conditions** (`when`):
- `$x : Type( c1, c2, … )` matches facts of `Type` that satisfy every constraint, joining with earlier bindings.
- `$v : field` inside a pattern binds a field.
- `not Type( … )` and `exists Type( … )`.
- `eval( expression )`.

**Expressions**:
- operators: `== != < <= > >=`, `+ - * / %`, `&& || !` (or `and`/`or`); `$x.field`.
- literals: numbers, `"strings"`, `true`/`false`/`null`.
- functions: `min max abs ceil floor round int float sqrt len str coalesce is_null`.
- Null-safe: comparing with `null` is false (except `==`/`!=`), arithmetic with `null` gives `null`, and dividing by zero gives `null`.

**Actions** (`then`): calls to actions from the scope's catalog. Arguments are expressions.

**Semantics**: each tick the manager builds the working memory (facts) and computes all
activations. The agenda is ordered by salience and then by file order. Activation groups
and cooldowns are applied, and the actions run in order. Effects are observed at the next tick.

### Facts and actions per scope (`GET /v1/contracts/catalog/{scope}` lists every field)

| Scope | Facts | Actions |
|---|---|---|
| `farm` | `Job` (status, workers, min/max_workers, budget_free, closed, …), `Metrics` (queue_depth, in_flight, outstanding, throughput, arrival_rate, avg/p95_latency, avg_service_time, failure_rate, live/busy_workers, utilisation, avg_utilisation, idle_seconds, elapsed, pod_failures, …) | `scale(n)`, `scale_up(k=1)`, `scale_down(k=1)`, `suspend()`, `resume()`, `escalate(reason, amount=1)`, `log(fmt, …)` |
| `data_parallel` | as farm, plus `Job.partitions` and `Metrics` partitions, pending_batches, pending_partitions, batches_done/failed, last_batch_time, partition_skew | as farm, plus `set_partitions(n)` |
| `group` | `Group` (max_workers, used_workers, free, jobs, active_jobs), `JobStat` (one per member), `Escalation` (job_id, reason, amount, age) | `scale_job(id, n)`, `suspend_job(id)`, `resume_job(id)`, `set_budget(n)`, `dismiss(id)`, `log(…)` |

### Built-in contracts

| Name | Scope | Behaviour (parameters) |
|---|---|---|
| `farm-throughput` (default) | farm | double workers on backlog, +1 on latency, −1 when under-used, back to minimum when idle (`backlog_per_worker`, `target_latency`, `low_utilisation`, `idle_after`) |
| `dp-balance` (default) | data_parallel | keep ≥ 1 partition per worker, split finer on skew, grow when partitions wait or batches are slow, shrink when idle (`partitions_per_worker`, `max_partitions`, `skew_limit`, `target_batch_time`, `idle_after`) |
| `group-fair-share` (default) | group | grant free budget to escalating members, reclaim from idle and under-used members (`reclaim_idle_after`) |

More examples: `examples/contracts/farm-latency-sla.drl` (SLA + protection against
failure storms + explicit escalation), `examples/contracts/dp-deadline.drl`.

### Escalation between the two levels

When a member's scale-up is clipped by the group budget, it raises a `budget`
escalation whose `amount` is the number of workers still missing. The amount shrinks as the
group grants workers, and the escalation disappears once the member reaches the size it
asked for or scales itself down. Rules can also call `escalate("reason", n)` explicitly.

---

## 6. Workers

```python
# mypkg/handlers.py
def process(payload):            # or process(payload, ctx) for job/task/attempt/partition info
    return do_work(payload)      # must return something JSON-serialisable
```

Run it with `python -m aje_worker mypkg.handlers:process`, or call `aje_worker.run(process)`.
Sync handlers run in a thread, so AMQP heartbeats keep flowing; `async def` handlers are awaited.
Extend `Dockerfile.worker` to package your code. An exception reports a failed attempt,
and the engine retries up to `max_retries`.

| Exit | Meaning |
|---|---|
| `0` | drained by the engine at the end of the job |
| `143` | SIGTERM (scale-down, suspension). The current task finishes within `AJE_TERM_GRACE` seconds, otherwise it's redelivered |

Wire protocol (`src/aje_worker/protocol.py`). Every message body is **msgpack**
(`application/vnd.msgpack`): it encodes any JSON value plus raw bytes, is smaller and
faster than JSON, and has libraries for every mainstream language. (BSON was the
alternative: it needs a document at the top level, so a task returning `25` would need
wrapping, and it spends more bytes per field.)

Per job, internal (deleted when the job ends):
- `aje.<job>.tasks` (durable classic queue)
- `aje.<job>.results` (results and heartbeats)
- `aje.<job>.control` (fanout exchange, `{"cmd": "drain"}`)

### Output streams

Accepted results are published to the job's **output stream**, the durable queue
`aje.output.<id>`. The id is independent of the job: set `output_queue` to share one stream
between jobs (every run of a schedule, say), or omit it and the engine generates a unique
id, returned as `output_queue` by the API. Every message carries the `job_id`:

| `kind` | when | fields (besides `v`, `output_id`, `job_id`, `job_name`, `pattern`, `ts`) |
|---|---|---|
| `task.result` | a farm task succeeded | `task_id`, `attempt`, `worker`, `started_at`, `finished_at`, `result` |
| `task.failed` | a farm task failed after its retries | `task_id`, `attempt`, `worker`, `error` |
| `batch.result` | a data-parallel batch was gathered | `batch_id`, `partitions`, `size`, `duration`, `result` |
| `batch.failed` | a batch failed | `batch_id`, `error` |
| `job.finished` | the job ended | `status`, `message`, `counts` |

Only accepted outputs are published: duplicate and stale deliveries are dropped, retries
are resolved first, and data-parallel partitions are published only once gathered.
Messages are persistent and confirmed by the broker. The AMQP `message_id`
(`<job>:<task>:<attempt>`, `<job>:<batch>`, `<job>:finished`) is an idempotency key,
since delivery is at least once. The `type` and the `job_id`/`kind` headers allow
filtering without decoding. The engine never deletes output streams.

```python
import aio_pika
from aje_worker import protocol

queue = await channel.declare_queue(protocol.output_queue("nightly-results"), durable=True)
async with queue.iterator() as messages:
    async for message in messages:
        async with message.process():
            out = protocol.unpack(message.body)   # {"kind": "task.result", "job_id": ..., ...}
```

### Job context and the PostgreSQL data plane

Every job can carry a `jobcontext`: any JSON configuration, plus an optional typed
`datastore` section. Every worker receives it in the `AJE_JOB_CONTEXT` environment variable
and reads it as `ctx.job_context`, a Pydantic model (`aje_worker.context.JobContext`).

```json
"jobcontext": {
  "power": 3,
  "model": {"name": "v2", "threshold": 0.8},
  "datastore": {
    "input": "dataplane.points",
    "output": "dataplane.results",
    "dsn_env": "AJE_DATASTORE_DSN",
    "dsn_secret": {"name": "aje-datastore", "key": "dsn"}
  }
}
```

```python
def power_rows(payload, ctx):                       # src/aje_worker/examples/datastore.py
    store = ctx.job_context.require_datastore()
    power = ctx.job_context.get("power", 2)
    with store.connect() as conn:                   # psycopg 3; DSN read from store.dsn_env
        ...                                         # SELECT from store.table("input"), UPSERT into store.table("output")
    return {"rows": n}                              # RabbitMQ carries only this
```

**PostgreSQL is the data plane, RabbitMQ the control plane.** Tasks carry references, such
as id ranges (`id_ranges(0, 1_000_000, 10_000)`), and results carry counts. The rows go
straight between the workers and PostgreSQL.

**Write idempotently.** A task can run twice (worker crash, scale-down), so use upserts or
`INSERT ... ON CONFLICT`. Redelivery is then harmless.

**No credentials in the context.** It is stored with the job and returned by the API, so
keys that look like secrets (`password`, `token`, `dsn`...) are rejected. The DSN reaches
the workers in one of three ways:
- **Kubernetes:** from a Secret, via `dsn_secret`, injected into the `dsn_env` variable.
- **Local backend:** from the API's environment, which local workers inherit.
- **Any backend:** from `worker.env`.

**Limits.** At most 64 KiB, since it travels as an environment variable. Table names must
be `table` or `schema.table`. `store.table()` returns safely quoted identifiers for
composing SQL.

---

## 7. Self-healing and durability

- **Task failure**: retried up to `max_retries`; then marked failed, and a data-parallel batch fails.
- **Worker crash**: the unacked task is redelivered by RabbitMQ; the backend replaces the worker. The job fails only if crashes exceed `backoff_limit`.
- **Orphaned tasks**: if tasks are outstanding while the queue is empty and every live worker is idle for 30 s (for example a result lost while the engine was down), they are dispatched again. Stale duplicate results are ignored by attempt number.
- **Engine restart**: jobs, tasks, batches, groups and contracts live in the database, and queues and messages are durable. On start, managers are rebuilt, outstanding work is reloaded, workers are recreated if missing, and results produced while the engine was down are consumed.

---

## 8. Configuration (`AJE_*` environment variables)

| Variable | Default | |
|---|---|---|
| `AJE_DATABASE_URL` | `postgresql://aje:aje@localhost:5432/aje` | PostgreSQL only (asyncpg); `postgresql+asyncpg://` is accepted too |
| `AJE_DB_POOL_SIZE` | `10` | connections in the engine's asyncpg pool (opened at start, never exceeded) |
| `AJE_AMQP_URL` | `amqp://guest:guest@localhost:5672/` | engine → RabbitMQ |
| `AJE_WORKER_AMQP_URL` | = `AJE_AMQP_URL` | address the **workers** use |
| `AJE_BACKEND` | `local` | `local` \| `kubernetes` |
| `AJE_K8S_NAMESPACE` / `AJE_K8S_IN_CLUSTER` / `AJE_K8S_KUBECONFIG` | `default` / `false` / – | |
| `AJE_K8S_TTL_AFTER_FINISHED` / `AJE_K8S_IMAGE_PULL_SECRET` | `3600` / – | |
| `AJE_MONITOR_INTERVAL` / `AJE_MONITOR_WINDOW` | `5` / `60` s | per-job defaults |
| `AJE_HEARTBEAT_INTERVAL` / `AJE_GROUP_INTERVAL` | `5` / `5` s | |
| `AJE_API_KEY` | – | require `X-API-Key` (or `?api_key=` for browser `EventSource`) |
| `AJE_DB_AUTO_CREATE` | `true` | apply the migrations at start; `false` when migrations own the schema (compose) |
| `AJE_CORS_ORIGINS` | `[]` | browser origins allowed to call the API, e.g. `["http://localhost:5173"]` |
| `AJE_DATASTORE_DSN` (worker side) | – | PostgreSQL DSN for `jobcontext.datastore` (name configurable with `dsn_env`) |

---

## 9. Development

```bash
pip install -e ".[dev]"
pytest src/__tests__/unit                # fast: no Docker
pytest -m integration                    # PostgreSQL + RabbitMQ in Testcontainers (needs Docker)
python -m aje.migrate                    # apply migrations to AJE_DATABASE_URL
# new migration: add src/aje/infrastructure/persistence/migrations/versions/vNNNN_name.py
#   (revision, down_revision, upgrade and downgrade SQL) and update the row models in db.py
uvicorn aje.main:app --reload            # needs RabbitMQ on localhost
ruff format src && ruff check src        # Google style: formatting, imports, naming, docstrings
(cd src && pylint --rcfile ../pylintrc aje aje_worker __tests__)   # Google's own pylintrc
```

Tests live in `src/__tests__/`:
- `unit/`: pure logic with no I/O. Anything that touches PostgreSQL or RabbitMQ is an
  integration test against the real thing: no fakes or mocks of the database or the broker.
- `integration/`: the real engine against PostgreSQL and RabbitMQ started by Testcontainers.
  Every test starts from a schema rebuilt by the migrations, and one test checks that
  the migrated schema matches the row models. Workers are in-process but speak AMQP, exactly like
  the worker image.
- `common/`: the shared test layer, one module per job: `specs` (job/group/worker builders),
  `probe` (`EngineProbe`: submit, wait, read persisted rows), `api` (`ApiClient`: one object per
  HTTP resource, `expect(response, status)`), `schema` (`SchemaAdmin`), `output` (`OutputReader`), `fakes` (in-process seams only),
  `polling`, `kubernetes`, `handlers`, `engine`, `containers`. Integration fixtures are layered in
  `integration/conftest.py` (`schema` -> `engine` -> `probe` / `api`); tests are grouped in classes
  by behaviour.

### Code style

The code follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html),
with one project exception: lines may be 110 characters (the guide says 80). It is enforced by:
- `ruff format`: formatting (Black-compatible, like Google's pyink).
- `ruff check`: rules mapped to guide sections in `pyproject.toml` (imports, naming,
  docstring format, comprehensions, logging, mutable defaults, ...).
- `pylint --rcfile pylintrc`: Google's published pylintrc. Two local changes are listed at the
  top of the file: the line length, and docstrings required for functions of 12 or more lines.
- `src/__tests__/unit/test_style.py`: what no linter checks. Imports name modules, not classes
  or functions (2.2), and there is no `@staticmethod` (2.17).

### Architecture

The code follows clean architecture in a `src` layout: dependencies point inwards, and
only the composition root knows the concrete classes.

```
src/aje/
  domain/              pure rules, no framework: errors, cron, dsl/ (contract language), contracts/ (built-in .drl)
  application/         use cases
    ports/             interfaces the application owns: backend (WorkerSpec, Backend), broker, repositories
    services/          one service per use case (jobs, groups, schedules, contracts, streams) + presenters
    runtime/           the autonomic runtime: engine, job/data-parallel/group managers, scheduler, event bus
    schemas.py         API DTOs (Pydantic v2)
  infrastructure/      adapters implementing the ports
    persistence/       db.py (Pydantic rows + asyncpg Store), repositories.py, migrations/ (SQL revisions)
    backends/          kubernetes, local, inprocess; registry.py picks one by AJE_BACKEND
    broker/            amqp (RabbitMQ), memory
  interfaces/http/     FastAPI: routers, deps (ServiceContainer protocol), error mapping
  config.py            settings (AJE_* environment)
  container.py         composition root: wires infrastructure into the runtime and the services
  main.py              HTTP composition root (uvicorn aje.main:app);  migrate.py  python -m aje.migrate
src/aje_worker/        worker SDK: runtime, AMQP transport, job context, examples (fractal, datastore)
src/__tests__/         unit/ integration/ common/
```

- **Dependency rule.** `src/__tests__/unit/test_architecture.py` checks every import: inner
  layers never import outer ones, the domain uses no framework, and the worker SDK never
  imports the engine. One seam remains and is listed explicitly in that test: the runtime
  and the presenters still persist through the Pydantic rows and `Store`. The next step is
  repository ports for the runtime's writes.
- **Dependency injection.** Routers receive their services through FastAPI dependencies
  (`interfaces/http/deps.py`). The HTTP app only knows a `ServiceContainer` Protocol, and
  `aje.main` hands it the real container. Tests can swap any provider with
  `app.dependency_overrides`.
- **Constructor injection.** Services and the runtime receive their collaborators in the
  constructor (`aje/container.py`).
- **Adding a backend.** Use `@register_backend("name")`; nothing else changes.

**Container images.** The API image runs from source in `/app/aje` (`PYTHONPATH=/app/aje`,
packages `aje` and `aje_worker`). The worker image keeps the SDK in `/app/ajeworker`; put
your handlers there too (`COPY my_handlers.py /app/ajeworker/`).

### Current limits

- One API replica: managers run in-process. Running replicas would need leader election, or sharding jobs by lease.
- Task payloads and results are stored in the database. For large data, pass references (object-store keys) instead.
- No per-task timeout yet: a handler that hangs keeps its task until that worker is scaled down, suspended or restarted, which puts the task back in the queue.
