# Client interface: submitting jobs and following them

How a UI submits work to the Autonomic Job Engine (AJE) and keeps a live view of it. Every
endpoint is also described by the OpenAPI document at `/docs` (`/openapi.json`). Background
and known limitations are in [job-status-mechanism.md](job-status-mechanism.md).

## 1. Basics

| | |
|---|---|
| Base URL | e.g. `http://localhost:8000` (all business endpoints are under `/v1`) |
| Authentication | Header `X-API-Key: <key>` when the server sets `AJE_API_KEY`. `EventSource` cannot set headers, so streams accept `?api_key=<key>` |
| CORS | The UI's origin must be listed in `AJE_CORS_ORIGINS`, e.g. `["http://localhost:5173"]` |
| Formats | JSON over HTTP; live updates as Server-Sent Events (`text/event-stream`) |
| Times | ISO 8601 with a time zone in JSON bodies; epoch seconds inside `metrics` |

Errors:

| Status | Meaning | Body |
|---|---|---|
| 400 | Valid request, impossible operation (e.g. tasks for a data-parallel job) | `{"detail": "..."}` |
| 401 | Missing or wrong API key | `{"detail": "missing or invalid X-API-Key"}` |
| 404 | No such job, group or schedule | `{"detail": "..."}` |
| 409 | Not possible in the current state (e.g. suspending a suspended job, adding tasks to a closed job) | `{"detail": "..."}` |
| 422 | Invalid request body | `{"detail": [ {loc, msg, type}, ... ]}`, or for contracts `{"detail": "invalid contract", "error": {"message", "line", "column"}}` |

## 2. Submitting work

### Submit a job: `POST /v1/jobs` → `201` with a [Job](#job)

```jsonc
// farm: independent tasks, one result each
{
  "name": "squares",                                  // DNS-1123 label: a-z, 0-9, '-'
  "pattern": "farm",
  "worker": {"command": ["python", "-m", "aje_worker", "mypkg.handlers:square"]},
  "max_workers": 8,
  "tasks": [1, 2, 3],                                  // optional first tasks
  "close_when_done": true,                             // finish once these are done
  "output_queue": "squares-results"                    // optional; see section 6
}
```

```jsonc
// data_parallel: each batch is split into partitions and gathered into one result
{
  "name": "sums", "pattern": "data_parallel",
  "worker": {"command": ["python", "-m", "aje_worker", "mypkg.handlers:sum_squares"]},
  "gather": "sum",                                     // list | concat | sum | merge
  "partitions": 4,
  "items": [1, 2, 3, 4, 5, 6, 7, 8]                     // optional first batch
}
```

The other fields (worker image, resources, contract, retries, `jobcontext`, ...) are in the
README under "Job request". Validate a contract before submitting it with
`POST /v1/contracts/validate`.

### Feed a running job

| Action | Request | Response |
|---|---|---|
| Add tasks (farm) | `POST /v1/jobs/{id}/tasks` `{"tasks": [...], "close": false}` | `202 {"task_ids": [...]}` |
| Add a batch (data-parallel) | `POST /v1/jobs/{id}/batches` `{"items": [...], "partitions": 4}` | `202 {"batch_id", "partitions"}` |
| No more input | `POST /v1/jobs/{id}/close` | Job |

A job without `close_when_done` keeps running, waiting for input, until it is closed
(`close`, or `"close": true` with the last tasks) or cancelled.

### Control a job

| Action | Request | Response |
|---|---|---|
| Cancel | `DELETE /v1/jobs/{id}` | Job (`409` if already finished) |
| Suspend / resume | `POST /v1/jobs/{id}/suspend` / `resume` | Job (`409` in the wrong state) |
| Scale | `POST /v1/jobs/{id}/scale` `{"workers": 3}` | Job (clamped to min/max and the group budget) |
| Partitions (data-parallel) | `POST /v1/jobs/{id}/partitions` `{"partitions": 8}` | Job (applies to the next batches) |
| Self-management on/off | `PUT /v1/jobs/{id}/management` `{"management": "active" \| "passive"}` | Job |
| Replace the contract | `PUT /v1/jobs/{id}/contract` `{"builtin": ...}` or `{"source": ...}` | Contract |

Groups (`/v1/groups`) and cron schedules (`/v1/schedules`) follow the same style. See
`/docs` for their endpoints.

## 3. Job lifecycle

```
pending ──▶ running ◀──▶ suspended
               │
               ▼ (input closed and all work done)
            draining ──▶ succeeded
any live state ──▶ failed     (workers failed)
any live state ──▶ cancelled  (DELETE, group or schedule)
```

`succeeded`, `failed` and `cancelled` are terminal. Each is announced by an event of the same
name: `job.succeeded`, `job.failed`, `job.cancelled`. The job's `message` says why it ended.

## 4. Following a job (recommended)

Open the job's SSE stream with the browser's `EventSource`. It delivers the job's **whole
history first**, then live updates, so the UI never has to fetch or merge the event log:

- **History, then live.** Every stored event comes with its id as the SSE `id`. If the
  connection drops, `EventSource` reconnects by itself and sends `Last-Event-ID`; the stream
  resumes right after it, with no gaps and no duplicates.
- **A clean end.** When the job ends, the stream sends `end` with the final status and
  closes. A reconnect after that gets `204 No Content`, which makes `EventSource` stop.
  Calling `close()` on `end` is still good practice.
- **Slow clients.** If the UI falls far behind (more than 1,000 queued messages), the stream
  sends `reset` and closes. `EventSource` reconnects and the replay fills the gap. Only `task`
  messages, which are live-only, can be missing after a `reset`: re-read
  `GET /v1/jobs/{id}/tasks` if you show them.

Query parameters of `GET /v1/jobs/{id}/stream`:

| Parameter | Default | |
|---|---|---|
| `metrics_every` | `5` | Seconds between `metrics` messages (min 0.5) |
| `tasks` | `false` | Also send a `task` message for every task result (live progress, results) |
| `after_id` | whole history | Start the replay after this event id (e.g. the last one the UI already shows) |
| `api_key` | – | The API key, when `EventSource` cannot send headers |

```ts
type AjeEvent = {id: number | null; ts: string; job_id: string | null; group_id: string | null;
                 kind: string; message: string; data: Record<string, unknown> | null};

export function watchJob(base: string, apiKey: string, jobId: string, ui: {
  event(e: AjeEvent): void;                              // timeline: history, then live
  metrics(m: Record<string, any>): void;                 // status, workers, counters, rates
  task?(t: AjeEvent): void;                              // per-task results (tasks=true)
  done(status: string): void;                            // succeeded | failed | cancelled
}) {
  const url = new URL(`${base}/v1/jobs/${jobId}/stream`);
  url.searchParams.set("metrics_every", "2");
  if (ui.task) url.searchParams.set("tasks", "true");
  url.searchParams.set("api_key", apiKey);

  const es = new EventSource(url);
  const data = (m: Event) => JSON.parse((m as MessageEvent).data);
  es.addEventListener("event", (m) => ui.event(data(m)));
  es.addEventListener("metrics", (m) => ui.metrics(data(m)));
  es.addEventListener("task", (m) => ui.task?.(data(m)));
  es.addEventListener("end", (m) => { es.close(); ui.done(data(m).status); });
  // "reset" needs no handler: the server closes, EventSource reconnects and resumes.
  return () => es.close();                               // call on unmount
}
```

Other options:

- **Many jobs at once (dashboard):** `GET /internal/events?snapshot_every=2` (filter with
  `job_id` or `group_id`) sends a `snapshot` of every live job every 2 s, every `event`, and
  with `tasks=true` a `task` message per task result. Events carry ids and a reconnect resumes
  after the last one. The first connection replays history only if you pass `after_id`,
  because this feed spans every job. The Fractal Lab demo uses this stream.
- **A group:** `GET /v1/groups/{id}/stream` replays the group's history (its own events and its
  members'), then sends the group's `facts` right away and every `metrics_every` s, then
  `end` when the group is finished or cancelled. Same `after_id`, `Last-Event-ID` and `204`
  rules as jobs. Adding a job to a finished group revives it: open the stream again.
- **No SSE available:** poll `GET /v1/jobs/{id}` (every 2–5 s) and
  `GET /v1/jobs/{id}/events?after_id=` (every 1–2 s); stop after a terminal status.

## 5. Progress

The `metrics` message (and `metrics` in the Job, and the `snapshot` entries) carries the live
counters. They are refreshed once per management tick (`monitor_interval`, default 5 s).

| Pattern | Progress | Notes |
|---|---|---|
| farm | `done = completed + failed`, `total = submitted` | `total` is final only once the job is closed (`closed: true`). Until then, show counts or throughput instead of a percentage. |
| data_parallel | Per batch: `GET /v1/jobs/{id}/batches/{bid}` → `completed_partitions / partitions`; per job: `batches_done`, `batches_failed`, `pending_batches` | `batch.scattered`, `batch.done` and `batch.failed` events mark each batch. |

`counts` in a Job has the same keys everywhere (`GET /v1/jobs/{id}`, `GET /v1/jobs`, group
members, snapshots, `metrics`), whether the job is live or finished: `submitted`,
`completed`, `failed`, `retried`, `outstanding`, plus `batches_pending`, `batches_done` and
`batches_failed` for data-parallel jobs. A finished job reports the values it had when it
ended.

## 6. Message reference

### <a id="job"></a>Job (`JobOut`)

`id, name, pattern, status, group_id, management, workers, min_workers, max_workers,
partitions, closed, message, created_at, updated_at, finished_at, contract, counts,
metrics, jobcontext, output_queue`

### SSE messages

| Stream | `event:` | `data` (JSON) |
|---|---|---|
| `/v1/jobs/{id}/stream` | `event` | An [event](#events); SSE `id` = event id (history first, then live) |
| | `metrics` | `{"status", "workers", ...metric keys}`. The first message may contain only `status` and `workers` |
| | `task` | With `tasks=true`: one per task result (see below). No SSE `id`: live only |
| | `end` | `{"job_id", "status"}`: the job is over (final status); the stream closes |
| | `reset` | `{"reason", "after_id"}`: the client fell behind; the stream closes and the reconnect resumes |
| `/v1/groups/{id}/stream` | `event` | An event of the group or a member job, with its SSE `id` |
| | `facts` | `{"Group": [...], "JobStat": [...], "Escalation": [...]}`, after the history and every `metrics_every` s |
| | `end` | `{"group_id", "status"}`: `finished` or `cancelled`; the stream closes |
| | `reset` | As for jobs |
| `/internal/events` | `snapshot` | `{"jobs": [{id, name, pattern, status, group_id, management, workers, closed, counts, metrics}]}` |
| | `event` | An [event](#events), with its SSE `id` |
| | `task` | With `tasks=true` (see below) |
| | `reset` | As for jobs |

A `task` message: `{"id": null, "kind": "task.done" | "task.failed", "job_id", "group_id",
"ts", "data": {task_id, worker, attempt, batch_id, partition, counts, result | error, service}}`.

A stream answers `204 No Content` instead of opening when the job (or group) is over and
there is nothing after the resume point. Browsers treat that as "stop reconnecting".

### <a id="events"></a>Events

`{"id": 42, "ts": "2026-11-23T10:00:00+00:00", "job_id", "group_id", "kind", "message", "data"}`.
`message` is a human-readable sentence; `data` holds the details. Filter by kind prefix with
`GET /v1/jobs/{id}/events?kind=job.`.

| Kind | Meaning |
|---|---|
| `job.submitted`, `job.started` | Accepted; workers started |
| `job.scaled` | Workers changed (`data`: old, new, requested, source) |
| `job.suspended`, `job.resumed` | Paused or resumed (`data.source`: api, rule, group, schedule) |
| `job.closed`, `job.draining` | Input closed; all work done, workers asked to exit |
| `job.succeeded`, `job.failed`, `job.cancelled` | **Terminal** |
| `job.recovered` | Engine restarted and resumed managing the job |
| `job.management`, `job.contract`, `job.repartitioned`, `job.escalated` | Management changes |
| `task.failed` | A task failed for good, after its retries (`data.task_id`) |
| `task.redispatched` | Orphaned tasks were sent again (self-healing) |
| `batch.scattered`, `batch.done`, `batch.failed` | Data-parallel batch lifecycle |
| `rule.log`, `rule.error`, `action.error` | Messages from the job's contract |
| `group.*`, `schedule.*` | Group and schedule changes, on their own streams |

### Metric keys

The schema is `JobMetrics` in the OpenAPI document (`/docs`), with a description per field.
It is used by `JobOut.metrics`, `GET /v1/jobs/{id}/metrics` (`JobMetricsOut`), the SSE
`metrics` messages (flattened, plus `status` and `workers`) and the `snapshot` entries.

All patterns:
- Queue and workers: `queue_depth`, `in_flight`, `outstanding`, `live_workers`, `busy_workers`, `utilisation`.
- Counters: `submitted`, `completed`, `failed`, `retried`.
- Rates and latencies: `throughput` (completions per minute), `arrival_rate`, `avg_latency`, `p95_latency`,
  `avg_service_time` (seconds), `failure_rate`, `avg_utilisation`.
- Other: `idle_seconds`, `elapsed`, `pod_failures`, `ts` (epoch seconds).

Data-parallel adds `partitions`, `pending_batches`, `pending_partitions`, `batches_done`,
`batches_failed`, `last_batch_time`, `partition_skew`. These are `null` for farm jobs.

`GET /v1/jobs/{id}/metrics` returns the same metrics plus a `history` of recent points (for
charts), the backend status and open escalations.

## 7. Results

- **In the UI:** `GET /v1/jobs/{id}/tasks?include_payload=false` (paged with `limit` and
  `offset`, filtered with `status=done|failed|queued`), `GET /v1/jobs/{id}/tasks/{task_id}`,
  and `GET /v1/jobs/{id}/batches/{bid}` for gathered batches.
- **Live, per task:** the `task` messages of `/internal/events?tasks=true`.
- **For backend services:** every job also publishes its accepted results to the RabbitMQ
  queue `aje.output.<output_queue>` as msgpack messages carrying `job_id`. See "Output
  streams" in the README. Browsers should not consume RabbitMQ directly.
