import { workerColor } from "../fractal.js";

function fmt(v, digits = 1) {
  return v === null || v === undefined ? "-" : Number(v).toFixed(digits);
}

export default function StatusPanel({ run, elapsed, onScale, onCancel, onToggleManagement }) {
  if (!run) {
    return (
      <section className="panel status">
        <h2>Status</h2>
        <p className="hint">Pick a fractal and press Launch. Every tile is a task of a farm job; watch the
          workers that compute them appear and disappear.</p>
      </section>
    );
  }
  const done = run.done;
  const pct = run.total ? Math.round((100 * done) / run.total) : 0;
  const m = run.metrics || {};
  const live = !["succeeded", "failed", "cancelled"].includes(run.status);
  const workers = Object.entries(run.workerTiles).sort((a, b) => b[1] - a[1]);

  return (
    <section className="panel status">
      <h2>Status <span className={`pill ${run.status}`}>{run.status}</span></h2>
      <div className="job-id" title="job id">{run.name} · {run.jobId}</div>

      <div className="progress"><div style={{ width: `${pct}%` }} /></div>
      <div className="row"><span>{done} / {run.total} tiles</span><span>{pct}%</span></div>

      <div className="stats">
        <div><b>{elapsed.toFixed(1)}s</b><span>elapsed</span></div>
        <div><b>{run.workers}</b><span>workers</span></div>
        <div><b>{run.peakWorkers}</b><span>peak</span></div>
        <div><b>{fmt(run.workerSeconds, 0)}</b><span>worker·s</span></div>
        <div><b>{fmt(m.throughput, 0)}</b><span>tiles/min</span></div>
        <div><b>{m.queue_depth ?? "-"}</b><span>queued</span></div>
        <div><b>{fmt(m.avg_service_time, 2)}s</b><span>per tile</span></div>
        <div><b>{m.utilisation === undefined ? "-" : `${Math.round(100 * m.utilisation)}%`}</b><span>busy</span></div>
      </div>

      {live && (
        <div className="actions">
          {run.management === "passive" ? (
            <>
              <button onClick={() => onScale(run.workers - 1)} disabled={run.workers <= 1}>− worker</button>
              <button onClick={() => onScale(run.workers + 1)} disabled={run.workers >= run.maxWorkers}>+ worker</button>
            </>
          ) : <span className="hint">The contract is in charge.</span>}
          <button className="ghost" onClick={onToggleManagement}>
            {run.management === "passive" ? "Hand over to contract" : "Take control"}
          </button>
          <button className="danger" onClick={onCancel}>Cancel</button>
        </div>
      )}

      <h3>Workers</h3>
      <ul className="workers">
        {workers.length === 0 && <li className="hint">waiting for the first tile...</li>}
        {workers.map(([id, n]) => (
          <li key={id}>
            <i style={{ background: workerColor(run.workerOrder, id) }} />
            <span className="wid" title={id}>{id}</span>
            <span className="bar"><span style={{ width: `${(100 * n) / Math.max(1, done)}%`,
              background: workerColor(run.workerOrder, id) }} /></span>
            <b>{n}</b>
          </li>
        ))}
      </ul>
    </section>
  );
}
