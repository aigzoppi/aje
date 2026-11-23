"""Kubernetes backend: the Job manifest built for workers."""

import pytest

from aje.infrastructure.backends import kubernetes
from common import kubernetes as kubernetes_lib
from common import specs


@pytest.fixture(name="job_manifest")
def job_manifest_fixture() -> dict:
    """A three-worker manifest with every optional setting filled in."""
    spec = specs.worker_spec(
        job_id="abc123",
        job_name="squares",
        image="reg/aje-worker:1",
        command=["python", "-m", "aje_worker", "aje_worker.examples:square"],
        env={"A": "1"},
        resources={"cpu_request": "100m", "memory_limit": "256Mi"},
        node_selector={"pool": "batch"},
        backoff_limit=7,
    )
    return kubernetes_lib.manifest(
        spec,
        3,
        namespace="jobs",
        amqp_url="amqp://rmq/",
        heartbeat=5,
        ttl_after_finished=600,
        image_pull_secret="regcred",
    )


class JobManifestTest:
    def test_is_a_work_queue_job(self, job_manifest):
        s = job_manifest["spec"]
        assert job_manifest["metadata"]["name"] == kubernetes.k8s_name("abc123") == "aje-abc123"
        assert s["parallelism"] == 3 and "completions" not in s
        assert s["backoffLimit"] == 7 and s["ttlSecondsAfterFinished"] == 600
        assert s["template"]["metadata"]["labels"]["aje.io/job-id"] == "abc123"

    def test_sigterm_exits_are_not_failures(self, job_manifest):
        rule = job_manifest["spec"]["podFailurePolicy"]["rules"][0]
        assert rule["onExitCodes"]["values"] == [143] and rule["action"] == "Ignore"

    def test_pod_settings(self, job_manifest):
        pod = job_manifest["spec"]["template"]["spec"]
        assert pod["restartPolicy"] == "Never" and pod["nodeSelector"] == {"pool": "batch"}
        assert pod["imagePullSecrets"] == [{"name": "regcred"}]

    def test_worker_container(self, job_manifest):
        c = kubernetes_lib.worker_container(job_manifest)
        assert c["name"] == "worker"
        assert c["resources"] == {"requests": {"cpu": "100m"}, "limits": {"memory": "256Mi"}}

    def test_worker_identity_comes_from_the_environment(self, job_manifest):
        env = kubernetes_lib.container_env(job_manifest)
        assert env["AJE_JOB_ID"]["value"] == "abc123"
        assert env["AJE_WORKER_ID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
