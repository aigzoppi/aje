# AJE Fractal Lab

A demo for the team: render the Mandelbrot or a Julia set with an **autonomic farm** and watch
the engine manage it live.

- Every tile of the image is one **task** of a farm job, computed by
  `aje_worker.examples.fractal:tile` (pure Python, so tiles take long enough to see scaling).
- The page listens to **`GET /internal/events?tasks=true`** (Server-Sent Events):
  - `task`: each finished tile, with the worker that computed it. The tile is painted as it arrives.
  - `snapshot`: the job's live state every second (workers, queue, throughput).
  - `event`: management decisions (rule firings, `job.scaled`, lifecycle changes).
- **The game.** Choose who drives the workers:
  - **Contract**: the built-in `farm-throughput` contract scales on backlog.
  - **You**: passive management; scale with `+` / `−` while it renders.
  
  The leaderboard scores each run by **tiles per worker-second**. Many workers is fast but
  wasteful; the goal is to be fast *and* frugal. Click the image to zoom in (×2) and render
  again, right click to zoom out.

## Run it

### With docker compose (from the repository root)

```bash
docker compose --profile demo up --build
open http://localhost:8080
```

### Development server

The API must be running (for example `docker compose up`). Then:

```bash
cd examples/demo
npm install
AJE_API_URL=http://localhost:8000 npm run dev    # http://localhost:5173
```

The Vite dev server proxies `/v1` and `/internal` to `AJE_API_URL`. If the API requires a key,
start Vite with `VITE_AJE_API_KEY=...`. `EventSource` cannot send headers, so the stream sends
the key as `?api_key=`.

## Files

| | |
|---|---|
| `src/App.jsx` | run lifecycle: submit the job, route stream messages, leaderboard |
| `src/api.js` | REST client and the `/internal/events` stream |
| `src/fractal.js` | views, zoom, tiles, payloads, palette (mirrors the worker's payload format) |
| `src/components/` | canvas, controls, status, event log, leaderboard |
