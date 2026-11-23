// Thin client for the AJE API (see docs/clientinterface.md).
// Requests go to the same origin: the Vite dev server proxies /v1 and /internal.
const API_KEY = import.meta.env.VITE_AJE_API_KEY || "";

async function request(method, path, body) {
  const headers = { "content-type": "application/json" };
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data?.detail ?? res.statusText;
    const msg = typeof detail === "string" ? detail : detail.map?.((d) => `${d.loc?.join(".")}: ${d.msg}`).join("; ");
    throw new Error(`${res.status} ${msg || JSON.stringify(detail)}`);
  }
  return data;
}

export const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

export const api = {
  listJobs: (limit = 500) => request("GET", `/v1/jobs?limit=${limit}`),
  getJob: (id) => request("GET", `/v1/jobs/${id}`),
  submitJob: (spec) => request("POST", "/v1/jobs", spec),
  cancelJob: (id) => request("DELETE", `/v1/jobs/${id}`),
  suspend: (id) => request("POST", `/v1/jobs/${id}/suspend`),
  resume: (id) => request("POST", `/v1/jobs/${id}/resume`),
  close: (id) => request("POST", `/v1/jobs/${id}/close`),
  scale: (id, workers) => request("POST", `/v1/jobs/${id}/scale`, { workers }),
  setManagement: (id, management) => request("PUT", `/v1/jobs/${id}/management`, { management }),
  addTasks: (id, tasks, close = false) => request("POST", `/v1/jobs/${id}/tasks`, { tasks, close }),
  addBatch: (id, items, partitions) =>
    request("POST", `/v1/jobs/${id}/batches`, partitions ? { items, partitions } : { items }),
};

function streamUrl(path, params) {
  const qs = new URLSearchParams(params);
  if (API_KEY) qs.set("api_key", API_KEY); // EventSource cannot send headers
  return `${path}?${qs}`;
}

// Dashboard feed: a `snapshot` of every live job every few seconds, plus every persisted `event`.
export const openDashboardStream = (snapshotEvery = 2) =>
  new EventSource(streamUrl("/internal/events", { snapshot_every: String(snapshotEvery) }));

// One job: its whole event history first, then live `event`, `metrics` and a final `end`.
export const openJobStream = (jobId, metricsEvery = 2) =>
  new EventSource(streamUrl(`/v1/jobs/${jobId}/stream`, { metrics_every: String(metricsEvery) }));
