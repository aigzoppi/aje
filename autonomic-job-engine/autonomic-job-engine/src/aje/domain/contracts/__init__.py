"""Built-in contracts (pre-defined management plans) shipped with the engine."""

from __future__ import annotations

import pathlib

_DIR = pathlib.Path(__file__).parent

BUILTINS = {
    "farm-throughput": ("farm", "farm-throughput.drl"),
    "dp-balance": ("data_parallel", "dp-balance.drl"),
    "group-fair-share": ("group", "group-fair-share.drl"),
}

DEFAULTS = {
    "farm": "farm-throughput",
    "data_parallel": "dp-balance",
    "group": "group-fair-share",
}


def builtin_source(name: str) -> str:
    if name not in BUILTINS:
        available = ", ".join(BUILTINS)
        raise KeyError(f"unknown built-in contract {name!r}; available: {available}")
    return (_DIR / BUILTINS[name][1]).read_text()


def builtin_scope(name: str) -> str:
    return BUILTINS[name][0]


def list_builtins() -> list[dict]:
    return [{"name": n, "scope": s, "source": builtin_source(n)} for n, (s, _) in BUILTINS.items()]
