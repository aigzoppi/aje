// Thin client for the AJE API. Requests go to the same origin (Vite proxy / nginx).
const API_KEY = import.meta.env.VITE_AJE_API_KEY || "";

async function request(method, path, body) {
  const headers = { "content-type": "application/json" };
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data?.detail ?? res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

export const api = {
  info: () => request("GET", "/v1/info"),
  submitJob: (spec) => request("POST", "/v1/jobs", spec),
  getJob: (id) => request("GET", `/v1/jobs/${id}`),
  cancelJob: (id) => request("DELETE", `/v1/jobs/${id}`),
  scale: (id, workers) => request("POST", `/v1/jobs/${id}/scale`, { workers }),
  setManagement: (id, management) => request("PUT", `/v1/jobs/${id}/management`, { management }),
  tasks: (id) => request("GET", `/v1/jobs/${id}/tasks?limit=5000`),
};

// Live firehose: snapshots, persisted events and (with tasks=true) every task result.
// EventSource cannot send headers, so the key travels as a query parameter.
export function openEventStream({ tasks = true, snapshotEvery = 1 } = {}) {
  const params = new URLSearchParams({ tasks: String(tasks), snapshot_every: String(snapshotEvery) });
  if (API_KEY) params.set("api_key", API_KEY);
  return new EventSource(`/internal/events?${params}`);
}
