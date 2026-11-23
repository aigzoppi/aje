import { useCallback, useEffect, useRef, useState } from "react";
import { api, openDashboardStream, TERMINAL } from "../api.js";

const POLL_MS = 5000;

// Keeps a map of jobs up to date:
//  * loads the list once with GET /v1/jobs,
//  * merges the live `snapshot` messages of /internal/events (live jobs only),
//  * re-reads a job when one of its job.* events arrives (catches terminal states,
//    which drop out of the snapshot),
//  * falls back to polling GET /v1/jobs while the stream is down.
export function useJobsFeed() {
  const [jobs, setJobs] = useState({});
  const [connection, setConnection] = useState("connecting"); // connecting | live | polling
  const [error, setError] = useState(null);
  const pending = useRef(new Set());
  const jobsRef = useRef(jobs);
  jobsRef.current = jobs;

  const upsert = useCallback((list) => {
    setJobs((prev) => {
      const next = { ...prev };
      for (const j of list) next[j.id] = { ...prev[j.id], ...j };
      return next;
    });
  }, []);

  const refresh = useCallback(async () => {
    try {
      upsert(await api.listJobs());
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, [upsert]);

  const refreshJob = useCallback(
    (id) => {
      if (pending.current.has(id)) return; // coalesce bursts of events
      pending.current.add(id);
      setTimeout(async () => {
        pending.current.delete(id);
        try {
          upsert([await api.getJob(id)]);
        } catch {
          /* the next snapshot or poll will catch up */
        }
      }, 300);
    },
    [upsert],
  );

  useEffect(() => {
    refresh();
    let poll = null;
    const startPolling = () => {
      if (!poll) poll = setInterval(refresh, POLL_MS);
    };
    const stopPolling = () => {
      clearInterval(poll);
      poll = null;
    };

    const es = openDashboardStream(2);
    es.onopen = () => {
      setConnection("live");
      stopPolling();
      refresh(); // fill anything missed while disconnected
    };
    es.onerror = () => {
      setConnection("polling");
      startPolling(); // EventSource keeps retrying on its own
    };
    es.addEventListener("snapshot", (m) => {
      const live = JSON.parse(m.data).jobs || [];
      upsert(live);
      // A job we think is live but is missing from the snapshot has ended: re-read it.
      const ids = new Set(live.map((j) => j.id));
      for (const j of Object.values(jobsRef.current)) {
        if (!TERMINAL.has(j.status) && !ids.has(j.id)) refreshJob(j.id);
      }
    });
    es.addEventListener("event", (m) => {
      const ev = JSON.parse(m.data);
      if (ev.job_id && ev.kind?.startsWith("job.")) refreshJob(ev.job_id);
    });

    return () => {
      es.close();
      stopPolling();
    };
  }, [refresh, refreshJob, upsert]);

  return { jobs: Object.values(jobs), connection, error, refresh, upsert };
}
