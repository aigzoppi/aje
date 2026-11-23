# Job status: how progress reaches the API and the clients

This document describes the mechanism (as of 2026-11-23), traced through the code, and lists
the problems a client ran into. Findings 1–6 and 8–10 are fixed; 7 and 11 remain open. The interface a UI should implement is in
[clientinterface.md](clientinterface.md).

## Summary

- **A live job's state lives in memory.** Each job has a `JobManager` inside the API process,
  holding its status, counters, outstanding tasks and latest metrics. Every change is also
  written to PostgreSQL (tables `jobs`, `tasks`, `batches`, `events`).
- **Every lifecycle or management decision is an *event*.** The in-process `EventBus` first
  persists it to the `events` table, then pushes it to subscribers. Per-task results are
  *notifications*. They are pushed only to subscribers that ask for them, and never stored as
  events.
- **Clients have four ways to follow a job:**
  1. polling the REST API;
  2. the per-job SSE stream;
  3. the group SSE stream, or the engine-wide SSE stream (`/internal/events`);
  4. for backend services, the RabbitMQ output stream (msgpack).

## How a job updates its progress

```
worker ──result──▶ aje.<job>.results ──▶ JobManager.on_message
                                          ├─ counters in memory (completed, failed, retried, outstanding)
                                          ├─ tasks row in PostgreSQL (status, result, error)
                                          ├─ output stream aje.output.<id> (task.result / task.failed)
                                          ├─ notification task.done / task.failed   (only if someone listens)
                                          └─ event task.failed                       (permanent failures)

every monitor_interval (default 5 s) ── JobManager.tick
                                          ├─ monitor(): metrics → last_metrics, history (720 points), Prometheus
                                          ├─ lifecycle: running → draining → succeeded | failed   (events + jobs row)
                                          ├─ self-healing: orphaned tasks re-sent                 (event task.redispatched)
                                          └─ contract actions: scale, suspend, ...                (events job.scaled, ...)
```

| What changes | When | Stored in | Exposed by |
|---|---|---|---|
| Status | On each transition | `jobs.status`, plus a `job.<status>` event | `GET /v1/jobs/{id}`, events, streams |
| Counters (submitted, completed, failed, retried, outstanding) | On every result | Memory; per-task rows in `tasks` | `JobOut.counts`, the `metrics` SSE message, snapshots |
| Metrics (queue depth, throughput, latency, utilisation, ...) | Every `monitor_interval` | Memory only (last value plus history) | `GET /v1/jobs/{id}/metrics`, the `metrics` SSE message, snapshots, Prometheus |
| Management decisions (scale, suspend, contract, escalation) | When they happen | `events` | Events: REST and SSE |
| Task results | On every result | `tasks.result` | `GET /v1/jobs/{id}/tasks`, `task` notifications (`/internal/events?tasks=true`), the RabbitMQ output stream |
| Batch results (data-parallel) | When gathered | `batches.result` | `GET /v1/jobs/{id}/batches/{bid}`, `batch.done` events, the output stream |

Status values: `pending` (only between submission and the first workers) → `running`
(⇄ `suspended`) → `draining` (input closed, all work done, workers asked to exit) →
`succeeded`. A job can also end `failed` (its workers failed) or `cancelled`. The three
terminal states each emit an event: `job.succeeded`, `job.failed`, `job.cancelled`.

## How a client is notified

| Channel | What it delivers | Freshness |
|---|---|---|
| `GET /v1/jobs/{id}` | Full view: live state from memory when managed, stored state otherwise | Live counters; metrics as of the last tick |
| `GET /v1/jobs/{id}/events?after_id=N` | Persisted events after `N`, oldest first | Immediate |
| `GET /v1/jobs/{id}/stream` (SSE) | The job's stored events (history, then live, with SSE ids); a `metrics` message at connect and every `metrics_every` s (default 5, min 0.5); `task` messages with `tasks=true`; `end` with the final status | Events immediate; metrics no fresher than the last tick (`monitor_interval`, default 5 s) |
| `GET /v1/groups/{id}/stream` (SSE) | History and live events of the group and its member jobs; the group's `facts` at connect and every `metrics_every` s; `end` when finished or cancelled | Same as above |
| `GET /internal/events` (SSE) | A `snapshot` of live jobs at connect and every `snapshot_every` s (default 2); every `event`; with `tasks=true`, a `task` message per task result (with counts) | Snapshots every 2 s; task messages immediate |
| RabbitMQ `aje.output.<id>` | Accepted task and batch results and `job.finished` (msgpack) | Immediate; meant for backend services, not browsers |

SSE connections get a keep-alive comment every 15 s (`sse-starlette` default). Every
endpoint requires `X-API-Key` when `AJE_API_KEY` is set. Browser `EventSource` cannot set
headers, so it may pass `?api_key=`. Browser origins must be listed in `AJE_CORS_ORIGINS`.

## Findings

| # | Severity | Finding | Recommendation |
|---|---|---|---|
| 1 | High | **A reconnect loses events.** SSE messages carry no `id:` field, so `EventSource` never sends `Last-Event-ID`, and the streams cannot resume. Events emitted while the client was disconnected are not replayed. | **Fixed.** Every stored event is sent with its id as the SSE `id`. Streams replay from the `events` table after `Last-Event-ID` (or `?after_id=`), then go live, skipping ids already sent. |
| 2 | High | **Connecting misses earlier events.** A stream only delivers events emitted after the subscription. A UI that submits a job and then opens the stream usually misses `job.submitted` and `job.started`. | **Fixed.** Job and group streams replay the whole history by default. `/internal/events` replays when given `after_id` or `Last-Event-ID`. |
| 3 | High | **Browsers reconnect forever after `end`.** The server closes the response after `end`. `EventSource` reconnects automatically (about 3 s later), gets `end` again, and so on. `end` also carries no final status. | **Fixed.** `end` carries the final status and is sent as soon as the terminal event is. A request after the end gets `204`, which stops `EventSource`. |
| 4 | Medium | **`counts` has three shapes.** A live job: `{submitted, completed, failed, retried, outstanding}` (plus `batches_pending/done/failed` for data-parallel). A finished job from `GET /v1/jobs/{id}`: task counts by status, e.g. `{done: 5, failed: 1}`. A finished job from `GET /v1/jobs`: `{}`. | **Fixed.** Final counters are stored on the job row (`jobs.counts`, migration 0002). Jobs finished before that get the same keys rebuilt from their tasks and batches. |
| 5 | Medium | **No per-task progress on the public job stream.** `task.done` / `task.failed` notifications are only available on `/internal/events?tasks=true`. The job stream reports progress only through `metrics`, at most every `monitor_interval`. | **Fixed.** `/v1/jobs/{id}/stream?tasks=true` sends a `task` message per result. |
| 6 | Medium | **Slow consumers lose messages silently.** Each subscriber has a bounded queue (1000); when it is full the oldest message is dropped with no signal. | **Fixed.** A full queue marks the subscription as overflowed instead of dropping messages. The stream sends `reset` and closes, and the reconnect replays the missed events (task messages are live-only). |
| 7 | Medium | **Streams only work on a single replica.** The event bus lives in the process that runs the engine. A second API replica would serve streams that receive nothing. | Fine with the current single-replica design. Scaling out needs a shared bus (PostgreSQL `LISTEN/NOTIFY` or a RabbitMQ fanout). |
| 8 | Low | **The group stream never ends,** and sends no initial state. Group managers are never removed from the engine, so a finished group keeps streaming `facts`. | **Fixed** (streaming). `facts` right after the history, `end` when the group is finished or cancelled, `204` afterwards. Finished group managers still stay in memory until restart. |
| 9 | Low | **An event that failed to persist looks like a task message.** It is still delivered, with `id: null`, and `/internal/events` classifies messages with `id == null` as `task`. | **Fixed.** The bus labels each message `event` or `task` explicitly. An unstored event stays an `event`. |
| 10 | Low | **The `metrics` payload is untyped.** It is the raw Metrics fact: its keys depend on the pattern and it has no schema in OpenAPI. | **Fixed.** `JobMetrics` (with `MetricsPoint`, `Escalation` and `JobMetricsOut`) is published in OpenAPI. Its field descriptions come from the contract catalog, and a test keeps the two in step. `JobOut.metrics`, `GET /v1/jobs/{id}/metrics`, the SSE `metrics` messages and the snapshots are all validated against it. Data-parallel fields are null for farms. |
| 11 | Low | **`?api_key=` exposes the key in URLs** (proxy and access logs). | Short-lived stream tokens, or cookie authentication for browsers. |

## Could SignalR (`signalrcore`) solve findings 1–3?

Short answer: **no, not by itself.** `signalrcore` is the wrong side of the connection, and
SignalR's reconnection features do not cover what findings 1 and 2 need.

### What `signalrcore` is

`signalrcore` (1.1.0, November 2026) is a **client** library: it connects a Python program to
an ASP.NET Core SignalR hub. It supports WebSockets, SSE and long polling, JSON and
MessagePack, automatic reconnection and streaming. It cannot host a hub, so it cannot replace
the SSE endpoints that AJE serves to browsers. Using SignalR would mean adding a separate
ASP.NET Core service as the hub:

```
AJE (Python) ──signalrcore──▶ ASP.NET Core SignalR hub ──▶ browsers (@microsoft/signalr)
```

### Finding by finding

| # | Finding | With SignalR |
|---|---|---|
| 1 | A reconnect loses events | **Partly.** ASP.NET Core 8 *stateful reconnect* buffers and replays messages during a *temporary* network interruption on the *same* connection. It is opt-in on both server and client, and buffers 100,000 bytes per connection by default. It does not help after a longer outage, a page reload, a new tab or a hub restart: the client gets a new connection and everything before it is gone. Also, `signalrcore` does not implement stateful reconnect yet (its PyPI page lists "Ack/Sequence implementation" as upcoming). So events AJE publishes while its own connection to the hub is reconnecting would be lost unless AJE buffers them. |
| 2 | Connecting misses earlier events | **No.** SignalR is live pub/sub with no history. Messages sent before a client connects are never delivered to it. The hub would still have to load the backlog from AJE (`GET /events?after_id=`) when a client joins. That is the same backfill the clients do today. |
| 3 | Browsers reconnect forever after `end` | **Yes, but there's a cheaper fix.** SignalR has an explicit connection close, and automatic reconnect is opt-in. The same result needs a few lines in the existing SSE endpoint: when the job is over, answer the reconnect with `204 No Content`. The HTML specification says `EventSource` then stops reconnecting. |

### Recommendation

The durable record of a job's history is already the `events` table. Findings 1–3 are best
fixed in the existing SSE endpoints, with no new service and the browser's built-in
`EventSource`:

1. Send every event with `id: <event id>`.
2. On connect, read `Last-Event-ID` (sent automatically by `EventSource` on reconnect) or
   `?after_id=`. Replay the missing events from the `events` table, then switch to live
   events, de-duplicating by id. This fixes findings 1 and 2 for any outage length, page
   reloads included.
3. When the job is no longer managed, send `end` with the final status. Answer any later
   reconnect with `204`. This fixes finding 3.

SignalR becomes worth considering only for different needs: two-way messaging, or fan-out
across several API replicas (finding 7, through a Redis backplane or Azure SignalR). Even then,
replaying missed events would still come from the `events` table.

Sources:
[signalrcore on PyPI](https://pypi.org/project/signalrcore/),
[signalrcore on GitHub](https://github.com/mandrewcito/signalrcore),
[ASP.NET Core SignalR configuration: stateful reconnect](https://learn.microsoft.com/en-us/aspnet/core/signalr/configuration),
[SignalR JavaScript client: automatic reconnect](https://learn.microsoft.com/en-us/aspnet/core/signalr/javascript-client).

## Code map

- `src/aje/application/runtime/job.py`: `on_message`, `tick`, `monitor`, `_lifecycle`, `_finish`.
- `src/aje/application/runtime/events.py`: `EventBus.emit` (persist, then push), `notify`, `subscribe`.
- `src/aje/application/services/streams.py`: the three SSE generators and `job_snapshot`.
- `src/aje/application/services/presenters.py`: `job_out`, live versus stored view.
- `src/aje/interfaces/http/jobs.py`, `groups.py`, `internal.py`: the endpoints.
