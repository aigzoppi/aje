"""Kubernetes backend: one batch/v1 Job per AJE job, in work-queue mode.

Mapping:
  workers      -> spec.parallelism (mutable on a running Job; excess pods are removed)
  suspend      -> spec.suspend=true (pods are terminated, queued tasks stay in RabbitMQ)
  completion   -> workers exit 0 after a "drain" broadcast; completions is left unset
                  so the Job completes when every pod has terminated after a success
  scale-down   -> pods receive SIGTERM and exit 143; a podFailurePolicy ignores 143
                  and DisruptionTarget so they do not count against backoffLimit
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio import config
from kubernetes_asyncio.client import rest

from aje.application.ports import backend
from aje_worker import protocol

log = logging.getLogger("aje.k8s")

LABEL_JOB = "aje.io/job-id"
LABEL_PATTERN = "aje.io/pattern"
MANAGED_BY = {"app.kubernetes.io/managed-by": "autonomic-job-engine"}


def k8s_name(job_id: str) -> str:
    return f"aje-{job_id}".lower()


def build_job_manifest(
    spec: backend.WorkerSpec,
    workers: int,
    *,
    namespace: str,
    amqp_url: str,
    heartbeat: float,
    ttl_after_finished: int,
    image_pull_secret: str | None = None,
) -> dict[str, Any]:
    """Builds the Kubernetes Job that runs a job's workers as a work queue.

    Workers pull tasks from RabbitMQ, so the Job has a ``parallelism`` and no
    ``completions``; SIGTERM exits (scale-down) are ignored by the failure policy.

    Args:
        spec: The workers to run.
        workers: Parallelism.
        namespace: Namespace of the Job.
        amqp_url: Broker URL the workers connect to.
        heartbeat: Heartbeat interval of the workers, in seconds.
        ttl_after_finished: Seconds a finished Job (and its pods) is kept.
        image_pull_secret: Secret for private registries, if any.

    Returns:
        The Job manifest.
    """
    labels = {
        **MANAGED_BY,
        LABEL_JOB: spec.job_id,
        LABEL_PATTERN: spec.pattern,
        "aje.io/job-name": spec.job_name[:63],
        **spec.labels,
    }
    env = [{"name": k, "value": str(v)} for k, v in spec.env.items()]
    env += [
        {"name": protocol.ENV_AMQP_URL, "value": amqp_url},
        {"name": protocol.ENV_JOB_ID, "value": spec.job_id},
        {"name": protocol.ENV_HEARTBEAT, "value": str(heartbeat)},
        {"name": protocol.ENV_WORKER_ID, "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
    ]
    if spec.job_context:
        env.append(
            {"name": protocol.ENV_JOB_CONTEXT, "value": json.dumps(spec.job_context, separators=(",", ":"))}
        )
        store = spec.job_context.get("datastore") or {}
        if store.get("dsn_secret"):  # the DSN comes from a Secret, never from the job spec
            ref = store["dsn_secret"]
            env.append(
                {
                    "name": store.get("dsn_env", "AJE_DATASTORE_DSN"),
                    "valueFrom": {"secretKeyRef": {"name": ref["name"], "key": ref.get("key", "dsn")}},
                }
            )
    resources: dict[str, dict[str, str]] = {}
    r = spec.resources or {}
    if r.get("cpu_request") or r.get("memory_request"):
        resources["requests"] = {
            k: v for k, v in (("cpu", r.get("cpu_request")), ("memory", r.get("memory_request"))) if v
        }
    if r.get("cpu_limit") or r.get("memory_limit"):
        resources["limits"] = {
            k: v for k, v in (("cpu", r.get("cpu_limit")), ("memory", r.get("memory_limit"))) if v
        }
    container: dict[str, Any] = {"name": "worker", "image": spec.image, "env": env}
    if spec.command:
        container["command"] = spec.command
    if spec.args:
        container["args"] = spec.args
    if resources:
        container["resources"] = resources
    if spec.image_pull_policy:
        container["imagePullPolicy"] = spec.image_pull_policy
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 30,
        "containers": [container],
    }
    if spec.node_selector:
        pod_spec["nodeSelector"] = spec.node_selector
    if spec.service_account:
        pod_spec["serviceAccountName"] = spec.service_account
    if image_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": image_pull_secret}]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": k8s_name(spec.job_id), "namespace": namespace, "labels": labels},
        "spec": {
            "parallelism": workers,
            "backoffLimit": spec.backoff_limit,
            "ttlSecondsAfterFinished": ttl_after_finished,
            "podFailurePolicy": {
                "rules": [
                    {
                        "action": "Ignore",
                        "onExitCodes": {
                            "containerName": "worker",
                            "operator": "In",
                            "values": [protocol.EXIT_TERMINATED],
                        },
                    },
                    {"action": "Ignore", "onPodConditions": [{"type": "DisruptionTarget"}]},
                ]
            },
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


class KubernetesBackend:
    """Runs each job's workers as one Kubernetes Job."""

    name = "kubernetes"

    def __init__(
        self,
        *,
        namespace: str,
        amqp_url: str,
        heartbeat: float = 5.0,
        in_cluster: bool = False,
        kubeconfig: str | None = None,
        ttl_after_finished: int = 3600,
        image_pull_secret: str | None = None,
    ):
        self.namespace = namespace
        self.amqp_url = amqp_url
        self.heartbeat = heartbeat
        self.in_cluster = in_cluster
        self.kubeconfig = kubeconfig
        self.ttl = ttl_after_finished
        self.image_pull_secret = image_pull_secret
        self._api: client.ApiClient | None = None

    async def start(self) -> None:
        if self.in_cluster:
            config.load_incluster_config()
        else:
            await config.load_kube_config(config_file=self.kubeconfig)
        self._api = client.ApiClient()
        self.batch = client.BatchV1Api(self._api)
        self.core = client.CoreV1Api(self._api)

    async def close(self) -> None:
        if self._api:
            await self._api.close()

    async def create(self, spec: backend.WorkerSpec, workers: int) -> None:
        body = build_job_manifest(
            spec,
            workers,
            namespace=self.namespace,
            amqp_url=self.amqp_url,
            heartbeat=self.heartbeat,
            ttl_after_finished=self.ttl,
            image_pull_secret=self.image_pull_secret,
        )
        await self.batch.create_namespaced_job(self.namespace, body)

    async def _patch(self, job_id: str, spec: dict) -> None:
        await self.batch.patch_namespaced_job(k8s_name(job_id), self.namespace, {"spec": spec})

    async def scale(self, job_id: str, workers: int) -> None:
        await self._patch(job_id, {"parallelism": workers})

    async def suspend(self, job_id: str) -> None:
        await self._patch(job_id, {"suspend": True})

    async def resume(self, job_id: str, workers: int) -> None:
        await self._patch(job_id, {"suspend": False, "parallelism": workers})

    async def delete(self, job_id: str) -> None:
        try:
            await self.batch.delete_namespaced_job(
                k8s_name(job_id), self.namespace, propagation_policy="Background"
            )
        except rest.ApiException as e:
            if e.status != 404:
                raise

    async def status(self, job_id: str) -> backend.BackendStatus:
        """Reads the Job's conditions and pod counts (phase ``missing`` when there is no Job)."""
        try:
            job = await self.batch.read_namespaced_job_status(k8s_name(job_id), self.namespace)
        except rest.ApiException as e:
            if e.status == 404:
                return backend.BackendStatus(phase="missing")
            raise
        st = job.status
        conds = {c.type: c for c in (st.conditions or []) if c.status == "True"}
        if "Failed" in conds:
            phase, msg = "failed", conds["Failed"].message
        elif "Complete" in conds:
            phase, msg = "complete", None
        elif "Suspended" in conds or job.spec.suspend:
            phase, msg = "suspended", None
        else:
            phase, msg = "running", None
        return backend.BackendStatus(
            phase=phase,
            active=st.active or 0,
            ready=st.ready or 0,
            succeeded=st.succeeded or 0,
            failed=st.failed or 0,
            message=msg,
        )

    async def workers(self, job_id: str) -> list[dict[str, Any]]:
        """Lists the Job's pods: name, state, node, exit code and start time."""
        pods = await self.core.list_namespaced_pod(self.namespace, label_selector=f"{LABEL_JOB}={job_id}")
        out = []
        for p in pods.items:
            cs = (p.status.container_statuses or [None])[0]
            exit_code = None
            if cs is not None and cs.state is not None and cs.state.terminated is not None:
                exit_code = cs.state.terminated.exit_code
            out.append(
                {
                    "name": p.metadata.name,
                    "state": "terminating"
                    if p.metadata.deletion_timestamp
                    else (p.status.phase or "").lower(),
                    "node": p.spec.node_name,
                    "exit_code": exit_code,
                    "started_at": p.status.start_time.timestamp() if p.status.start_time else None,
                }
            )
        return out

    async def logs(self, job_id: str, worker: str | None, tail: int) -> dict[str, str]:
        out = {}
        for w in await self.workers(job_id):
            if worker and w["name"] != worker:
                continue
            try:
                out[w["name"]] = await self.core.read_namespaced_pod_log(
                    w["name"], self.namespace, container="worker", tail_lines=tail
                )
            except rest.ApiException as e:
                out[w["name"]] = f"<logs unavailable: {e.reason}>"
        return out
