"""Engine settings from the environment (``AJE_*``) or a ``.env`` file."""

from __future__ import annotations

import functools

import pydantic
import pydantic_settings


class Settings(pydantic_settings.BaseSettings):
    """Every setting; the environment variable is the field name, upper-cased, prefixed AJE_."""

    model_config = pydantic_settings.SettingsConfigDict(env_prefix="AJE_", env_file=".env", extra="ignore")

    # storage (PostgreSQL through an asyncpg pool)
    database_url: str = "postgresql://aje:aje@localhost:5432/aje"
    # connections in the pool (AJE_DB_POOL_SIZE)
    db_pool_size: int = pydantic.Field(default=10, ge=1)
    # apply the migrations at start; set false where they run separately (python -m aje.migrate)
    db_auto_create: bool = True

    # messaging
    amqp_url: str = "amqp://guest:guest@localhost:5672/"
    # URL the workers use; differs from amqp_url when workers run in a cluster
    worker_amqp_url: str | None = None

    # execution backend
    backend: str = "local"  # see aje.backends.registry (local | kubernetes | any registered name)
    k8s_namespace: str = "default"
    k8s_in_cluster: bool = False
    k8s_kubeconfig: str | None = None
    k8s_ttl_after_finished: int = 3600
    k8s_image_pull_secret: str | None = None
    local_log_dir: str = "/tmp/aje-workers"

    # autonomic loop defaults
    monitor_interval: float = 5.0
    monitor_window: float = 60.0
    heartbeat_interval: float = 5.0
    group_interval: float = 5.0

    # API
    api_key: str | None = None
    # browser origins allowed to call the API (e.g. ["http://localhost:5173"] for the demo UI)
    cors_origins: list[str] = []
    log_level: str = "INFO"

    @property
    def effective_worker_amqp_url(self) -> str:
        return self.worker_amqp_url or self.amqp_url


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()
