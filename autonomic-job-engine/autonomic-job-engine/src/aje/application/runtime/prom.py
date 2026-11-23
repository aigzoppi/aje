"""Prometheus metrics exported at /metrics, labelled by job (and by scope for rules and actions)."""

import prometheus_client

_L = ["job_id", "job_name", "pattern"]

WORKERS = prometheus_client.Gauge("aje_job_workers", "Desired workers", _L)
ACTIVE = prometheus_client.Gauge("aje_job_active_workers", "Workers running according to the backend", _L)
QUEUE_DEPTH = prometheus_client.Gauge("aje_job_queue_depth", "Tasks waiting in the queue", _L)
OUTSTANDING = prometheus_client.Gauge("aje_job_outstanding", "Tasks queued or in flight", _L)
THROUGHPUT = prometheus_client.Gauge("aje_job_throughput_per_minute", "Successful tasks per minute", _L)
LATENCY = prometheus_client.Gauge("aje_job_avg_latency_seconds", "Mean enqueue-to-result latency", _L)
UTILISATION = prometheus_client.Gauge("aje_job_utilisation", "Average worker utilisation over the window", _L)
TASKS = prometheus_client.Counter("aje_tasks_total", "Finished task attempts", _L + ["outcome"])
RULES = prometheus_client.Counter(
    "aje_rule_firings_total", "Contract rule firings", ["scope", "owner_id", "rule"]
)
ACTIONS = prometheus_client.Counter(
    "aje_actions_total", "Management actions executed", ["scope", "owner_id", "action", "source"]
)


def remove_job(labels: tuple[str, str, str]) -> None:
    for g in (WORKERS, ACTIVE, QUEUE_DEPTH, OUTSTANDING, THROUGHPUT, LATENCY, UTILISATION):
        try:
            g.remove(*labels)
        except KeyError:
            pass
