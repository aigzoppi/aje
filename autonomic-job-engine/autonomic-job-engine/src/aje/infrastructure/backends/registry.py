"""Backend registry: maps ``AJE_BACKEND`` to a factory (open for extension, closed for modification).

A new execution backend registers itself without touching the composition root:

    @register_backend("nomad")
    def _nomad(settings: Settings) -> Backend:
        from aje.infrastructure.backends.nomad import NomadBackend
        return NomadBackend(...)

Factories import their implementation lazily, so optional dependencies (kubernetes_asyncio)
are only needed when that backend is selected.
"""

from __future__ import annotations

from collections.abc import Callable

from aje import config
from aje.application.ports import backend
from aje.infrastructure.backends import kubernetes
from aje.infrastructure.backends import local

BackendFactory = Callable[[config.Settings], backend.Backend]

_FACTORIES: dict[str, BackendFactory] = {}


def register_backend(name: str) -> Callable[[BackendFactory], BackendFactory]:
    def decorator(factory: BackendFactory) -> BackendFactory:
        _FACTORIES[name] = factory
        return factory

    return decorator


def available_backends() -> list[str]:
    return sorted(_FACTORIES)


def create_backend(settings: config.Settings) -> backend.Backend:
    try:
        factory = _FACTORIES[settings.backend]
    except KeyError:
        available = ", ".join(available_backends())
        raise ValueError(f"unknown backend {settings.backend!r}; available: {available}") from None
    return factory(settings)


@register_backend("local")
def _local(settings: config.Settings) -> backend.Backend:
    return local.LocalProcessBackend(
        settings.effective_worker_amqp_url, settings.local_log_dir, settings.heartbeat_interval
    )


@register_backend("kubernetes")
def _kubernetes(settings: config.Settings) -> backend.Backend:
    return kubernetes.KubernetesBackend(
        namespace=settings.k8s_namespace,
        amqp_url=settings.effective_worker_amqp_url,
        heartbeat=settings.heartbeat_interval,
        in_cluster=settings.k8s_in_cluster,
        kubeconfig=settings.k8s_kubeconfig,
        ttl_after_finished=settings.k8s_ttl_after_finished,
        image_pull_secret=settings.k8s_image_pull_secret,
    )
