import { useEffect, useState } from "react";
import { openJobStream } from "../api.js";

const MAX_EVENTS = 500;

// Follows one job via GET /v1/jobs/{id}/stream: history first, then live events and metrics.
export function useJobStream(jobId) {
  const [events, setEvents] = useState([]);
  const [metrics, setMetrics] = useState(null);
  const [ended, setEnded] = useState(null);

  useEffect(() => {
    if (!jobId) return undefined;
    setEvents([]);
    setMetrics(null);
    setEnded(null);
    const es = openJobStream(jobId, 2);
    es.addEventListener("event", (m) => {
      const ev = JSON.parse(m.data);
      setEvents((prev) => (prev.some((p) => p.id === ev.id) ? prev : [...prev, ev].slice(-MAX_EVENTS)));
    });
    es.addEventListener("metrics", (m) => setMetrics(JSON.parse(m.data)));
    es.addEventListener("end", (m) => {
      es.close();
      setEnded(JSON.parse(m.data).status);
    });
    return () => es.close();
  }, [jobId]);

  return { events, metrics, ended };
}
