"""Render Kubernetes worker manifests with test defaults and read them back."""

from typing import Any

from aje.application.ports import backend
from aje.infrastructure.backends import kubernetes

DEFAULTS = {"namespace": "n", "amqp_url": "amqp://x", "heartbeat": 1, "ttl_after_finished": 60}


def manifest(spec: backend.WorkerSpec, workers: int = 1, **options: Any) -> dict:
    return kubernetes.build_job_manifest(spec, workers, **{**DEFAULTS, **options})


def worker_container(m: dict) -> dict:
    return m["spec"]["template"]["spec"]["containers"][0]


def container_env(m: dict) -> dict[str, dict]:
    """The worker container's environment, by variable name."""
    return {e["name"]: e for e in worker_container(m)["env"]}
