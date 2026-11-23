"""Job context: the configuration every worker of a job receives.

A job is submitted with a ``jobcontext`` object. The engine stores it with the job and
hands it to every worker (env ``AJE_JOB_CONTEXT``, JSON); handlers read it from
``ctx.job_context``:

    {
      "jobcontext": {
        "power": 3,                          # any configuration parameter, any JSON value
        "model": {"name": "v2", "threshold": 0.8},
        "datastore": {                       # optional: PostgreSQL as the data plane
          "input": "dataplane.points",
          "output": "dataplane.squares",
          "dsn_env": "AJE_DATASTORE_DSN",            # where the worker finds the DSN
          "dsn_secret": {"name": "aje-datastore"}    # Kubernetes: fill dsn_env from this Secret
        }
      }
    }

Data plane vs control plane: with a datastore, workers read their input from PostgreSQL
and write their output there; RabbitMQ only carries small references (task payloads such
as id ranges, results such as row counts). Credentials never appear in the context,
because the context is stored and returned by the API. Only the *name* of the variable
or Secret holding the DSN does.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

import psycopg
from psycopg import sql
import pydantic

from aje_worker import protocol

MAX_CONTEXT_BYTES = 64 * 1024  # it travels as an environment variable

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}(\.[A-Za-z_][A-Za-z0-9_]{0,62})?$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRETISH = re.compile(r"(password|passwd|secret|token|dsn|credential)", re.IGNORECASE)


class SecretRef(pydantic.BaseModel):
    """A key of a Kubernetes Secret."""

    model_config = pydantic.ConfigDict(extra="forbid")

    name: str = pydantic.Field(..., pattern=r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
    key: str = "dsn"


class PostgresDataStore(pydantic.BaseModel):
    """Where a job's data lives. Tables are ``table`` or ``schema.table``."""

    model_config = pydantic.ConfigDict(extra="forbid")

    kind: Literal["postgres"] = "postgres"
    input: str | None = pydantic.Field(None, description="table or view the workers read from")
    output: str | None = pydantic.Field(None, description="table the workers write to")
    dsn_env: str = pydantic.Field(
        "AJE_DATASTORE_DSN", description="environment variable holding the DSN in the worker"
    )
    dsn_secret: SecretRef | None = pydantic.Field(
        None,
        description="Kubernetes backend: inject dsn_env from this Secret (otherwise set it via worker.env "
        "or the worker's environment)",
    )
    options: dict[str, Any] = pydantic.Field(
        {}, description="free-form datastore options for the handler (batch size, isolation level...)"
    )

    @pydantic.field_validator("input", "output")
    @classmethod
    def _identifier(cls, v: str | None) -> str | None:
        if v is not None and not _IDENT.match(v):
            raise ValueError(f"{v!r} is not a table name (use 'table' or 'schema.table')")
        return v

    @pydantic.field_validator("dsn_env")
    @classmethod
    def _env_name(cls, v: str) -> str:
        if not _ENV.match(v):
            raise ValueError(f"{v!r} is not an environment variable name")
        return v

    # ---------------------------------------------------------------- worker side
    def dsn(self) -> str:
        value = os.environ.get(self.dsn_env)
        if not value:
            raise RuntimeError(f"datastore DSN not found: set the environment variable {self.dsn_env}")
        return value.replace("postgresql+asyncpg://", "postgresql://", 1)

    def table(self, which: Literal["input", "output"]):
        """The table as a safe SQL identifier (``psycopg.sql.Identifier``) for query composition."""
        name = getattr(self, which)
        if not name:
            raise RuntimeError(f"the job context has no datastore.{which} table")
        return sql.Identifier(*name.split("."))

    def connect(self, **kwargs: Any):
        """A psycopg (v3) connection. Use it in a ``with`` block (commit on success)."""
        return psycopg.connect(self.dsn(), **kwargs)

    async def connect_async(self, **kwargs: Any):
        return await psycopg.AsyncConnection.connect(self.dsn(), **kwargs)


class JobContext(pydantic.BaseModel):
    """Arbitrary configuration parameters for the job, plus an optional typed datastore."""

    model_config = pydantic.ConfigDict(extra="allow")

    datastore: PostgresDataStore | None = None

    @property
    def parameters(self) -> dict[str, Any]:
        """Every free-form parameter (everything except the typed sections)."""
        return dict(self.model_extra or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.parameters.get(key, default)

    def require_datastore(self) -> PostgresDataStore:
        if self.datastore is None:
            raise RuntimeError("this handler needs a 'datastore' section in the job context")
        return self.datastore

    def to_env(self) -> str:
        return self.model_dump_json(exclude_none=True)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> JobContext:
        raw = (environ if environ is not None else os.environ).get(protocol.ENV_JOB_CONTEXT)
        return cls.model_validate_json(raw) if raw else cls()

    def check(self) -> JobContext:
        """Engine-side checks at submission: size, and no obvious secrets in free-form keys."""
        if len(self.to_env().encode()) > MAX_CONTEXT_BYTES:
            raise ValueError(
                f"jobcontext is larger than {MAX_CONTEXT_BYTES // 1024} KiB; "
                "put large data in the datastore and pass a reference"
            )
        leaked = [k for k in _walk_keys(self.parameters) if _SECRETISH.search(k)]
        if leaked:
            raise ValueError(
                "jobcontext must not carry credentials (keys " + ", ".join(sorted(leaked)) + "); "
                "it is stored and returned by the API. Use datastore.dsn_env / dsn_secret or "
                "worker.env instead"
            )
        return self


def _walk_keys(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for k, v in value.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            yield path
            yield from _walk_keys(v, path)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_keys(v, f"{prefix}[{i}]")


__all__ = ["JobContext", "PostgresDataStore", "SecretRef", "MAX_CONTEXT_BYTES"]
