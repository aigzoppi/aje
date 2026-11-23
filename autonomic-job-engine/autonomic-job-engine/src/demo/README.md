# AJE Jobs Console (React + MUI)

A small web console for the Autonomic Job Engine: submit jobs and track them live in a data grid.

- **Grid** (`@mui/x-data-grid`): every job with status, progress, workers, throughput, queue depth
  and creation time. Sort, filter, quick search, CSV export and per-row actions
  (details, suspend/resume, cancel). Double-click a row to open it.
- **Submit dialog**: a form for `farm` and `data_parallel` jobs (worker command, workers, retries,
  management, gather, partitions, first tasks/items, output queue), or a raw JSON tab for any
  field of the job request.
- **Detail drawer**: live metrics, the job's event timeline, and controls to scale, change
  management, suspend/resume, cancel, add tasks or batches, and close the input.

## How tracking works

Follows [docs/clientinterface.md](../../docs/clientinterface.md):

| What | Source |
|---|---|
| Initial list | `GET /v1/jobs?limit=500` |
| Live grid updates | `GET /internal/events?snapshot_every=2`: `snapshot` messages for live jobs |
| Final states | a `job.*` event, or a job dropping out of the snapshot, triggers `GET /v1/jobs/{id}` |
| Stream down | `EventSource` reconnects by itself. Meanwhile the grid polls `GET /v1/jobs` every 5 s |
| Detail drawer | `GET /v1/jobs/{id}/stream?metrics_every=2`: history, then live events/metrics, then `end` |

## Run

```bash
cd src/demo
npm install
AJE_API_URL=http://localhost:8000 npm run dev    # http://localhost:5174
```

The Vite dev server proxies `/v1` and `/internal` to `AJE_API_URL`, so you don't need CORS.
If the API needs a key, start the dev server with `VITE_AJE_API_KEY=<key>`. Streams send it as
`?api_key=` because `EventSource` cannot set headers.

`npm run build` writes a static bundle to `dist/`. Serve it behind a proxy that forwards `/v1`
and `/internal` to the API (see `examples/demo/nginx.conf`).
