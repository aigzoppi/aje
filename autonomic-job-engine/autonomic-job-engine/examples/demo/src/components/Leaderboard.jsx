// Best runs per configuration. Score = tiles per worker-second: fast AND frugal wins.
export default function Leaderboard({ entries, onClear }) {
  const sorted = [...entries].sort((a, b) => b.score - a.score).slice(0, 10);
  return (
    <section className="panel leaderboard">
      <h2>Leaderboard {entries.length > 0 && <button className="link" onClick={onClear}>clear</button>}</h2>
      <p className="hint">Score = tiles per worker-second. Throwing 16 workers at it is fast, but not efficient.
        Beat the contract by driving the workers yourself.</p>
      <table>
        <thead>
          <tr><th>#</th><th>driver</th><th>fractal</th><th>time</th><th>peak</th><th>worker·s</th><th>score</th></tr>
        </thead>
        <tbody>
          {sorted.length === 0 && <tr><td colSpan="7" className="hint">No finished runs yet.</td></tr>}
          {sorted.map((e, i) => (
            <tr key={e.at} className={e.mode}>
              <td>{i + 1}</td>
              <td>{e.mode === "autonomic" ? "contract" : "you"}</td>
              <td>{e.kind} {e.size}px · {e.maxIter} it</td>
              <td>{e.seconds.toFixed(1)}s</td>
              <td>{e.peakWorkers}</td>
              <td>{e.workerSeconds.toFixed(0)}</td>
              <td><b>{e.score.toFixed(2)}</b></td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
