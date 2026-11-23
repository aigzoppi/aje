import { JULIA_PRESETS } from "../fractal.js";

export default function Controls({ params, setParams, running, onLaunch, onReset }) {
  const set = (key) => (e) => {
    const v = e.target.type === "range" || e.target.type === "number" ? Number(e.target.value) : e.target.value;
    setParams((p) => ({ ...p, [key]: v }));
  };
  const tiles = Math.ceil(params.size / params.tileSize) ** 2;

  return (
    <section className="panel controls">
      <h2>Render</h2>

      <div className="segmented">
        {["mandelbrot", "julia"].map((k) => (
          <button key={k} className={params.kind === k ? "on" : ""} disabled={running}
                  onClick={() => onReset(k)}>{k === "mandelbrot" ? "Mandelbrot" : "Julia"}</button>
        ))}
      </div>

      {params.kind === "julia" && (
        <label>Julia constant
          <select disabled={running} value={params.c.join(",")}
                  onChange={(e) => setParams((p) => ({ ...p, c: e.target.value.split(",").map(Number) }))}>
            {JULIA_PRESETS.map((j) => <option key={j.name} value={j.c.join(",")}>{j.name} ({j.c[0]}, {j.c[1]}i)</option>)}
          </select>
        </label>
      )}

      <label>Iterations <b>{params.maxIter}</b>
        <input type="range" min="64" max="3000" step="32" value={params.maxIter} disabled={running}
               onChange={set("maxIter")} />
      </label>

      <label>Image
        <select value={params.size} disabled={running} onChange={set("size")}>
          {[256, 384, 512, 640].map((s) => <option key={s} value={s}>{s} x {s}</option>)}
        </select>
      </label>

      <label>Tile
        <select value={params.tileSize} disabled={running} onChange={set("tileSize")}>
          {[32, 48, 64, 96, 128].map((s) => <option key={s} value={s}>{s} px</option>)}
        </select>
      </label>
      <p className="hint">{tiles} tasks for the farm</p>

      <h2>Who drives?</h2>
      <div className="segmented">
        <button className={params.mode === "autonomic" ? "on" : ""} disabled={running}
                onClick={() => setParams((p) => ({ ...p, mode: "autonomic" }))}>Contract</button>
        <button className={params.mode === "manual" ? "on" : ""} disabled={running}
                onClick={() => setParams((p) => ({ ...p, mode: "manual" }))}>You</button>
      </div>
      <p className="hint">
        {params.mode === "autonomic"
          ? "The farm-throughput contract adds workers while the backlog grows and removes idle ones."
          : "Passive management: scale with + / - while it renders. Can you beat the contract?"}
      </p>

      <label>Max workers <b>{params.maxWorkers}</b>
        <input type="range" min="1" max="16" value={params.maxWorkers} disabled={running}
               onChange={set("maxWorkers")} />
      </label>

      {params.mode === "autonomic" && (
        <label>Backlog per worker <b>{params.backlogPerWorker}</b>
          <input type="range" min="1" max="20" value={params.backlogPerWorker} disabled={running}
                 onChange={set("backlogPerWorker")} />
        </label>
      )}

      <button className="launch" disabled={running} onClick={onLaunch}>
        {running ? "Rendering..." : "Launch"}
      </button>
      <button className="ghost" disabled={running} onClick={() => onReset(params.kind)}>Reset view</button>
    </section>
  );
}
