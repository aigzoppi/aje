const ICON = { job: "◆", rule: "⚙", task: "✕", schedule: "⏱", group: "▣", action: "!" };

export default function EventLog({ events }) {
  return (
    <section className="panel events">
      <h2>Management events</h2>
      <ol>
        {events.length === 0 && <li className="hint">Rule firings, scaling decisions and lifecycle changes appear here.</li>}
        {events.map((e) => (
          <li key={e.id} className={e.kind.replace(/\./g, "-")}>
            <time>{new Date(e.ts).toLocaleTimeString()}</time>
            <span className="kind">{ICON[e.kind.split(".")[0]] || "•"} {e.kind}</span>
            <span className="msg">{e.message}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
