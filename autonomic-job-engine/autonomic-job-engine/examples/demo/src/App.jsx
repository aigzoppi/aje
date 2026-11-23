import { useCallback, useEffect, useRef, useState } from "react";
import { api, openEventStream } from "./api.js";
import { defaultView, splitImage, tilePayloads, WORKER_COMMAND, workerColor, zoomView } from "./fractal.js";
import Controls from "./components/Controls.jsx";
import FractalCanvas from "./components/FractalCanvas.jsx";
import StatusPanel from "./components/StatusPanel.jsx";
import EventLog from "./components/EventLog.jsx";
import Leaderboard from "./components/Leaderboard.jsx";

const TERMINAL = ["succeeded", "failed", "cancelled"];
const BOARD_KEY = "aje-fractal-board";

function loadBoard() {
  try {
    return JSON.parse(localStorage.getItem(BOARD_KEY)) || [];
  } catch {
    return [];
  }
}

function saveBoard(entries) {
  try {
    localStorage.setItem(BOARD_KEY, JSON.stringify(entries));
  } catch {
    /* private mode: the board just is not remembered */
  }
}

export default function App() {
  const [params, setParams] = useState({
    kind: "mandelbrot", c: [-0.8, 0.156], view: defaultView("mandelbrot"), maxIter: 512, size: 512,
    tileSize: 64, mode: "autonomic", maxWorkers: 8, backlogPerWorker: 4,
  });
  const [run, setRun] = useState(null);
  const [events, setEvents] = useState([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(null);
  const [showWorkers, setShowWorkers] = useState(true);
  const [now, setNow] = useState(Date.now());
  const [board, setBoard] = useState(loadBoard);

  const canvas = useRef(null);
  const runRef = useRef(null); // the live run, readable from the SSE handlers
  const early = useRef([]); // messages that arrived before POST /v1/jobs returned the job id

  const update = useCallback((fn) => {
    if (!runRef.current) return;
    runRef.current = fn(runRef.current);
    setRun(runRef.current);
  }, []);

  // ------------------------------------------------------------------ live stream handlers
  const onTask = useCallback((ev) => {
    const r = runRef.current;
    if (ev.kind === "task.failed") {
      setError(`tile failed: ${ev.data.error}`);
      return;
    }
    const worker = ev.data.worker || "?";
    const order = r.workerOrder.includes(worker) ? r.workerOrder : [...r.workerOrder, worker];
    if (!canvas.current.addTile(ev.data.result, workerColor(order, worker))) return;
    update((p) => ({
      ...p, done: p.done + 1, workerOrder: order,
      workerTiles: { ...p.workerTiles, [worker]: (p.workerTiles[worker] || 0) + 1 },
    }));
  }, [update]);

  const finish = useCallback(async (status) => {
    const r = runRef.current;
    if (!r || r.finishedAt) return;
    update((p) => ({ ...p, status, finishedAt: Date.now() }));
    // Reconcile: paint any tile whose live message was missed (e.g. a reconnect).
    try {
      for (const t of await api.tasks(r.jobId)) {
        if (t.status === "done" && t.result && !canvas.current.has(t.result.tile)) {
          canvas.current.addTile(t.result, workerColor(r.workerOrder, t.worker));
          update((p) => ({ ...p, done: p.done + 1 }));
        }
      }
    } catch (e) {
      setError(e.message);
    }
    const final = runRef.current;
    if (status === "succeeded") {
      const seconds = (final.finishedAt - final.startedAt) / 1000;
      const workerSeconds = Math.max(final.workerSeconds, 0.001);
      const entry = {
        at: Date.now(), mode: final.mode, kind: final.kind, size: final.size, maxIter: final.maxIter,
        seconds, peakWorkers: final.peakWorkers, workerSeconds, score: final.total / workerSeconds,
      };
      setBoard((b) => {
        const next = [...b, entry].slice(-50);
        saveBoard(next);
        return next;
      });
    }
  }, [update]);

  const onEvent = useCallback((ev) => {
    setEvents((list) => [ev, ...list].slice(0, 200));
    const status = ev.kind.startsWith("job.") ? ev.kind.slice(4) : null;
    if (TERMINAL.includes(status)) finish(status);
  }, [finish]);

  const onSnapshot = useCallback((jobs) => {
    const r = runRef.current;
    const j = r && jobs.find((x) => x.id === r.jobId);
    if (!j) return;
    update((p) => {
      const t = Date.now();
      const running = !p.finishedAt;
      return {
        ...p, workers: j.workers, management: j.management, metrics: j.metrics || p.metrics,
        status: p.finishedAt ? p.status : j.status,
        peakWorkers: Math.max(p.peakWorkers, j.workers),
        workerSeconds: p.workerSeconds + (running ? (p.workers * (t - p.lastSnapshot)) / 1000 : 0),
        lastSnapshot: t,
      };
    });
    if (TERMINAL.includes(j.status)) finish(j.status);
  }, [update, finish]);

  const dispatch = useCallback((type, ev) => {
    const r = runRef.current;
    if (type === "snapshot") return onSnapshot(ev.jobs);
    if (!r) return;
    if (!r.jobId) {
      early.current.push([type, ev]); // replayed once the job id is known
      if (early.current.length > 5000) early.current.shift();
      return;
    }
    if (ev.job_id !== r.jobId) return;
    if (type === "task") onTask(ev);
    else onEvent(ev);
  }, [onSnapshot, onTask, onEvent]);

  useEffect(() => {
    const es = openEventStream({ tasks: true, snapshotEvery: 1 });
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false); // EventSource reconnects by itself
    for (const type of ["snapshot", "event", "task"]) {
      es.addEventListener(type, (e) => dispatch(type, JSON.parse(e.data)));
    }
    return () => es.close();
  }, [dispatch]);

  useEffect(() => {
    if (!run || run.finishedAt) return undefined;
    const id = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(id);
  }, [run]);

  // ------------------------------------------------------------------ commands
  async function launch(p = params) {
    setError(null);
    setEvents([]);
    early.current = [];
    const tiles = splitImage(p.size, p.size, p.tileSize);
    canvas.current.reset(tiles);
    const startedAt = Date.now();
    runRef.current = {
      jobId: null, name: `fractal-${startedAt.toString(36)}`, mode: p.mode, kind: p.kind, size: p.size,
      maxIter: p.maxIter, maxWorkers: p.maxWorkers, total: tiles.length, done: 0, status: "pending",
      management: p.mode === "manual" ? "passive" : "active", workers: 1, peakWorkers: 1, workerSeconds: 0,
      workerTiles: {}, workerOrder: [], metrics: null, startedAt, lastSnapshot: startedAt, finishedAt: null,
    };
    setRun(runRef.current);
    try {
      const job = await api.submitJob({
        name: runRef.current.name,
        pattern: "farm",
        worker: { command: WORKER_COMMAND },
        workers: 1, min_workers: 1, max_workers: p.maxWorkers,
        management: runRef.current.management,
        contract: { builtin: "farm-throughput", globals: { backlog_per_worker: p.backlogPerWorker, idle_after: 5 } },
        monitor_interval: 1, monitor_window: 15,
        tasks: tilePayloads(p),
        close_when_done: true,
      });
      update((r) => ({ ...r, jobId: job.id, status: job.status, workers: job.workers }));
      const buffered = early.current;
      early.current = [];
      for (const [type, ev] of buffered) dispatch(type, ev);
    } catch (e) {
      setError(e.message);
      update((r) => ({ ...r, status: "failed", finishedAt: Date.now() }));
    }
  }

  function resetView(kind) {
    setParams((p) => ({ ...p, kind, view: defaultView(kind) }));
  }

  function zoom(px, py, factor) {
    const next = { ...params, view: zoomView(params.view, [params.size, params.size], px, py, factor) };
    setParams(next);
    launch(next);
  }

  async function command(fn) {
    try {
      const job = await fn();
      if (job?.workers !== undefined) update((r) => ({ ...r, workers: job.workers, management: job.management }));
    } catch (e) {
      setError(e.message);
    }
  }

  const running = !!run && !run.finishedAt;
  const elapsed = run ? ((run.finishedAt || now) - run.startedAt) / 1000 : 0;

  return (
    <div className="app">
      <header>
        <h1>AJE <span>Fractal Lab</span></h1>
        <p>An autonomic farm renders the image: one task per tile, workers scaled by a contract (or by you).</p>
        <span className={`conn ${connected ? "up" : "down"}`}>{connected ? "live" : "connecting..."}</span>
      </header>

      {error && <div className="error" onClick={() => setError(null)}>{error}</div>}

      <main>
        <Controls params={params} setParams={setParams} running={running} onLaunch={() => launch()}
                  onReset={resetView} />

        <section className="stage">
          <FractalCanvas ref={canvas} size={params.size} showWorkers={showWorkers} onZoom={zoom}
                         disabled={running} />
          <div className="stage-bar">
            <label><input type="checkbox" checked={showWorkers} onChange={(e) => setShowWorkers(e.target.checked)} />
              outline tiles by worker</label>
            <span className="hint">click to zoom in · right click to zoom out</span>
          </div>
        </section>

        <StatusPanel run={run} elapsed={elapsed}
                     onScale={(n) => command(() => api.scale(run.jobId, n))}
                     onCancel={() => command(() => api.cancelJob(run.jobId))}
                     onToggleManagement={() => command(() => api.setManagement(
                       run.jobId, run.management === "passive" ? "active" : "passive"))} />
      </main>

      <footer>
        <EventLog events={events} />
        <Leaderboard entries={board} onClear={() => { saveBoard([]); setBoard([]); }} />
      </footer>
    </div>
  );
}
