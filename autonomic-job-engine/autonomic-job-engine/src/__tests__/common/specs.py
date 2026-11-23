"""Builders for the specifications tests submit.

Every builder starts from the same minimal valid body, so a test only states what it is
about. ``*_body`` functions return API JSON; ``farm``/``dp``/``group`` return the models the
engine takes directly.
"""

from typing import Any

from aje.application import schemas
from aje.application.ports import backend


def template(pattern: str = "farm", handler: str = "square", **fields: Any) -> dict:
    """A job without a name: what a schedule stores and a group member extends."""
    return {"pattern": pattern, "worker": {"command": [handler]}, **fields}


def farm_body(name: str = "f", handler: str = "square", **fields: Any) -> dict:
    return {"name": name, **template("farm", handler, **fields)}


def dp_body(name: str = "d", handler: str = "sum_squares", **fields: Any) -> dict:
    return {"name": name, **template("data_parallel", handler, **fields)}


def farm(name: str = "f", handler: str = "square", **fields: Any) -> schemas.JobCreate:
    return schemas.JobCreate(**farm_body(name, handler, **fields))


def dp(name: str = "d", handler: str = "sum_squares", **fields: Any) -> schemas.JobCreate:
    return schemas.JobCreate(**dp_body(name, handler, **fields))


def group(*jobs: schemas.JobCreate, name: str = "g", **fields: Any) -> schemas.GroupCreate:
    return schemas.GroupCreate(name=name, jobs=list(jobs), **fields)


def worker_spec(**fields: Any) -> backend.WorkerSpec:
    base = {"job_id": "abc", "job_name": "j", "pattern": "farm", "image": "img", "command": ["x"]}
    return backend.WorkerSpec(**{**base, **fields})
