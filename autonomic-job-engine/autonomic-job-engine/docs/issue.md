# Design issues

A review of the code base as of 2026-11-23. Each issue gives its evidence in the code, its
impact and a recommendation. Status-streaming issues are covered separately in
[job-status-mechanism.md](job-status-mechanism.md); its two open findings (7: several
replicas, 11: API key in URLs) are not repeated here.

| # | Severity | Issue |
|---|---|---|
| 1 | Critical | Nothing stops two engines from managing the same jobs |
| 2 | Critical | Local workers inherit the API server's secrets |
| 3 | High | One shared API key gives full control, including images, commands and service accounts |
| 4 | High | Contract evaluation is unbounded and runs on the API's event loop |
| 5 | High | Output streams leak durable queues, and nothing is ever cleaned up |
| 6 | High | Writes to PostgreSQL and RabbitMQ are not atomic (no outbox) |
| 7 | Medium | Job state is mutated concurrently: only part of it is under the job's lock |
| 8 | Medium | The runtime depends directly on the persistence layer |
| 9 | Medium | `JobManager` does too much |
| 10 | Medium | Every tick opens an AMQP channel per job |
| 11 | Medium | Any `ValueError` becomes HTTP 400 |
| 12 | Medium | In-memory state is changed before the database write succeeds |
| 13 | Low | Durations are measured with the wall clock |
| 14 | Low | The engine's lifecycle is tied to the API process |
| 15 | Low | Short job ids, and no handling of id collisions |
| 16 | Low | Results are binary-capable on the wire but JSON-only in storage |

---

## 1. Nothing stops two engines from managing the same jobs (Critical)

**Evidence.** Each process running `aje.main:app` starts a full engine
(`AutonomicEngine.start`). It recovers every `running`/`suspended` job and group, consumes
their result queues, and starts the cron scheduler. There is no leader election or instance
lock. The only advisory lock in the code is the one that serialises migrations.

**Impact.** Two engines on one database do the following:
- Both rebuild a manager for every live job, then scale and suspend it independently.
- They compete for the same result queue, so each sees only part of the results. Counters,
  gathering and the lifecycle go wrong; a batch may never be gathered.
- Both fire every cron schedule.

This can happen just by running `uvicorn --workers 2`, by scaling the deployment to two
replicas, or briefly during a rolling update.

**Recommendation.** At start, take a session-level `pg_try_advisory_lock` on a dedicated
connection. If it isn't granted, either refuse to start or run as a stateless read-only API
replica (see #14). Release it on stop. Document `replicas: 1` and the `Recreate` deployment
strategy until then.

## 2. Local workers inherit the API server's secrets (Critical)

**Evidence.** `LocalProcessBackend.create` builds the worker environment from
`dict(os.environ)` (`infrastructure/backends/local.py:90`), then runs `worker.command` from the
job spec as a subprocess of the API server.

**Impact.** Every worker process sees `AJE_DATABASE_URL` (the engine's own database
credentials), `AJE_API_KEY`, the AMQP credentials and anything else in the server's
environment. With the local backend, anyone who can submit a job runs arbitrary code inside
the API container with those secrets. That is the compose default.

**Recommendation.** Start workers from an explicit allow-list: `PATH`, locale, the `AJE_*`
worker variables from `aje_worker.protocol`, `spec.env`, and the datastore DSN variable when
the job declares one. Treat the local backend as development-only, and document that its
workers are trusted code.

## 3. One shared API key gives full control (High)

**Evidence.** Authentication is a single `AJE_API_KEY` compared in `deps.require_api_key`.
There are no users, roles or ownership. A job spec chooses the container `image`, `command`,
`env`, `node_selector` and `service_account` (`WorkerSpec`), and the Kubernetes backend
applies them as given.

**Impact.**
- Any client holding the key can run any image with any service account in the job
  namespace, which can escalate to that account's RBAC permissions.
- Any client can cancel or reconfigure every other user's jobs.
- Audit events don't say who did what.

**Recommendation.** Use per-client identities (OIDC/JWT, or one key per client mapped to a
principal), record the principal on jobs and events, and authorise per job or group. Keep
server-side allow-lists of images, service accounts and node selectors (per tenant if
needed), and reject anything else at validation time.

## 4. Contract evaluation is unbounded and runs on the API's event loop (High)

**Evidence.** `RuleBase._match` evaluates conditions by nested iteration over facts: a rule
with *k* patterns over *n* facts costs O(nᵏ). A contract may have any number of rules and
patterns; only syntax, names and arity are checked (`_validate_rule`). Rules run inside
`tick()` on the same asyncio loop that serves HTTP, SSE and result consumption.

**Impact.** A group contract joining `JobStat` with itself three times over 200 members means
8 million matches per tick. That blocks the event loop: the API stops answering, heartbeats
time out, and every job stops being managed. It can be done by mistake, or on purpose by
anyone with the API key.

**Recommendation.** Cap contract size at validation time (rules, patterns per rule,
expression depth). Bound evaluation (an activation or step budget per run, then abort the
run with a `rule.error`). Consider evaluating in a worker thread with a timeout, so the loop
stays responsive.

## 5. Output streams leak durable queues, and nothing is ever cleaned up (High)

**Evidence.**
- Every job gets an output stream. When `output_queue` is omitted, the engine generates a
  fresh id, and so a fresh durable queue `aje.output.<uuid>` (`engine.create_job`). The engine
  never deletes output queues, by design, and declares them without `x-expires` or length
  limits (`AmqpBroker.declare_output`).
- `events`, `tasks` and `batches` have no retention. Task payloads and results are stored
  in full, and only `jobcontext` is size-limited (64 KiB). `TasksIn`/`BatchIn` set a minimum
  size but no maximum.

**Impact.**
- RabbitMQ: every job without a consumer leaves a durable queue that keeps all its results
  until someone deletes it. Over weeks this fills the broker's disk and memory, and its alarms
  then block all publishing, including the engine's own tasks.
- PostgreSQL: the tables grow without bound, so replay and listing queries slow down.
- One very large request (a 100 MB batch) travels through the API, the database and RabbitMQ.

**Recommendation.**
- Create an output stream only when the client asks for one (`output_queue` set, or
  `"output": true`).
- Declare output queues with `x-expires` (idle lifetime) and optionally `x-max-length` /
  `x-overflow`, configurable per stream.
- Add a retention task, or partition `events`/`tasks` by time, keeping finished jobs' detail
  for N days.
- Set request size limits (number of tasks and bytes) and a maximum result size in the worker
  runtime.

## 6. Writes to PostgreSQL and RabbitMQ are not atomic (High)

**Evidence.**
- `JobManager._dispatch` stores the task rows, then publishes the tasks to RabbitMQ.
- Results are published to the output stream before the task row is updated (deliberate).
- `EventBus.emit` writes each event inline, on the management path.
- Cancellation updates rows, then deletes queues.

**Impact.**
- A crash between the two writes leaves `queued` tasks in the database that were never
  published. After a restart they are reloaded as outstanding, and only the orphan healer
  re-sends them, once live workers have sat idle for `ORPHAN_GRACE` (30 s or more). Until then
  the job stalls. The recovery happens by accident rather than by design.
- Output messages can be duplicated (documented at-least-once delivery).
- Every event costs a synchronous database round trip inside management decisions.

**Recommendation.** Use a transactional outbox. Write the task rows, an outbox row per message
and the events in one transaction; a relay publishes outbox rows to RabbitMQ and marks them
sent. The relay is also the natural place for the `NOTIFY` that several replicas need.

## 7. Job state is mutated concurrently (Medium)

**Evidence.** The per-job `_lock` is taken by `tick()`, `cancel()` and `set_contract()` only.
API control calls (`set_workers`, `suspend`, `resume`, `set_management`, `close`) and the
result handler (`on_message`) run unlocked, and interleave with a tick at every `await`.

**Impact.** Ordering bugs that are hard to reproduce. For example: an API `suspend` awaits
`backend.suspend` while the job's status is still `running`. A tick's contract action
`set_workers` then sees `running` and calls `backend.scale`, re-creating workers for a job
that ends up `suspended`. (Group budgets are safe today, because `allowance` and the
assignment of the new count run with no `await` in between. That property is easy to break
by accident.)

**Recommendation.** Serialise every mutation of a job through one entry point: take the lock
in the control interface too, or turn each manager into an actor that drains a command queue.

## 8. The runtime depends directly on the persistence layer (Medium)

**Evidence.** `test_architecture.py` lists nine `KNOWN_SEAMS`. The runtime (`engine`, `job`,
`data_parallel`, `group`, `scheduler`, `events`) and the presenters import
`infrastructure.persistence.db`, building row models and calling `Store` directly. Ports
exist only for the services' reads.

**Impact.** The core of the system can't be exercised without PostgreSQL, and its rules are
tangled with SQL details (`update_where`, column names). Changing persistence, for example
adding the outbox (#6), touches the runtime everywhere.

**Recommendation.** Add write-side ports (`JobStore`, `TaskStore`, ... or a unit-of-work port)
for what the runtime needs ("record a result", "start a batch"). Implement them in
infrastructure, and remove the seams one by one (the architecture test tracks progress).

## 9. `JobManager` does too much (Medium)

**Evidence.** `application/runtime/job.py` has 893 lines and 48 methods. One class handles
result ingestion, retries, persistence, output publishing, the MAPE loop, metrics and
Prometheus export, lifecycle supervision, self-healing, contract execution, the control
interface and escalation. `DataParallelManager` changes behaviour by overriding hooks in
that one class.

**Impact.** Each change risks the rest. Concurrency (#7) is hard to reason about, unit testing
is impractical, and adding a pattern means subclassing the whole manager.

**Recommendation.** Split it into collaborators the manager coordinates:
- a `ResultHandler` (ingestion, retries, outputs);
- a `Monitor` (metrics, history, export);
- a `Lifecycle` (draining, finishing, healing);
- an `Executor` (contract actions);
- a pattern strategy (farm or data-parallel) instead of inheritance.

## 10. Every tick opens an AMQP channel per job (Medium)

**Evidence.** `AmqpBroker.queue_stats` opens a channel, declares the queue passively and
closes the channel, on every `monitor()` call, which runs every `monitor_interval` (5 s) for
every job. Every worker also sends a heartbeat through the results queue every 5 s.

**Impact.** With hundreds of jobs: hundreds of channel open/close round trips every few
seconds, extra broker load and a flood of logs. Heartbeats grow with the total number of
workers, and each one goes through the same consumer as the results.

**Recommendation.** Reuse one long-lived channel for passive declares, or read the queue
depths in bulk from the RabbitMQ management API. Consider deriving liveness from consumer
counts and the results themselves, instead of separate heartbeats.

## 11. Any `ValueError` becomes HTTP 400 (Medium)

**Evidence.** `interfaces/http/errors.py` maps every `ValueError` to `400 Bad Request` (the
handler also covers `InvalidOperationError`, which subclasses it).

**Impact.** A bug that raises `ValueError` deep inside the engine (a bad cast, a malformed
row) is reported to the client as *their* mistake. It is never logged as a server error, so
it stays hidden.

**Recommendation.** Raise dedicated domain errors (`InvalidOperationError`, a new
`ValidationError`) for client-caused conditions, map only those to 400, and let other
exceptions become logged 500s.

## 12. In-memory state is changed before the database write succeeds (Medium)

**Evidence.**
- `Scheduler._save` sets the attributes on the in-memory row, then updates the database.
- Managers update `self.workers`, `self.status`, ... and then persist them.

**Impact.** If the database write fails, memory and storage disagree, and the API serves the
in-memory version until a restart silently reverts it. For schedules this can mean a
`next_run_at` that only exists in memory.

**Recommendation.** Persist first, then update memory (or roll memory back on failure). For
schedules, reload the row after a failed write.

## 13. Durations are measured with the wall clock (Low)

**Evidence.** The runtime uses `time.time()` 18 times and `time.monotonic()` never, including
for rule cooldowns, drain timeouts, orphan grace periods, escalation lifetimes and the
metrics window.

**Impact.** A clock step (NTP correction, VM resume) can fire cooldowns early, end drains
immediately, or freeze metrics windows.

**Recommendation.** Use `time.monotonic()` for every interval and timeout, and keep the wall
clock for timestamps that are shown or stored.

## 14. The engine's lifecycle is tied to the API process (Low)

**Evidence.** The engine starts and stops in the FastAPI lifespan (`interfaces/http/app.py`).
Scheduling, management and HTTP serving share one process and one event loop.

**Impact.** Every API deployment restarts management and cuts all streams. The API can't be
scaled without also scaling the engine (#1). A slow request handler or contract (#4) delays
management for every job.

**Recommendation.** Long term, run the engine as its own process: one leader, holding the lock
from #1. Run the HTTP API as stateless replicas that read from PostgreSQL, stream through
`LISTEN/NOTIFY` (finding 7 of the status document) and send commands to the engine through a
command queue.

## 15. Short job ids, and no handling of id collisions (Low)

**Evidence.** Job, group and schedule ids are `uuid4().hex[:12]`, which is 48 bits. They are
inserted with no retry.

**Impact.** A collision becomes a primary-key error, and the client gets an HTTP 500. The odds
are small, but they grow quickly with volume: about 1 in 10⁶ after 25,000 ids, and
near-certain beyond tens of millions.

**Recommendation.** Use 16+ hex characters (or UUIDv7, which also sorts by time), or retry on a
unique violation.

## 16. Results are binary-capable on the wire but JSON-only in storage (Low)

**Evidence.** The wire format is msgpack, which carries raw bytes. Results are stored in JSON
columns, so the worker runtime rejects anything that isn't JSON-compatible
(`aje_worker/runtime.py`). Binary results, such as the fractal tiles, must be base64 strings.

**Impact.** That costs about 33% extra bandwidth and storage, and the wire format's main
advantage for binary data goes unused.

**Recommendation.** Decide deliberately. Either keep JSON-only results (and say so in the
worker docs), or allow bytes: store them in a `bytea` column or object storage, put a
reference in the JSON, and pass them through msgpack unchanged.

---

## Suggested order

1. **Now:** #1 (instance lock), #2 (worker environment allow-list), #5 (opt-in output
   streams with `x-expires`, request size limits) and #11. All are small, contained changes.
2. **Next:** #3 (identities and allow-lists), #4 (contract limits), #6 (outbox) and #7
   (serialised mutations).
3. **Refactoring track:** #8 and #9 together, since write ports make the split easier. Then
   #14, which builds on #1 and #6.
