"""The dependency rule of the clean architecture, checked on every import of src/aje.

    domain          <- application <- infrastructure, interfaces <- composition root
    (pure rules)       (use cases,     (adapters: DB, broker,       (config, container,
                        ports)          backends, HTTP)              main, migrate)

Inner layers never import outer ones. ``KNOWN_SEAMS`` lists the remaining exceptions
explicitly; anything else fails, so a new violation cannot slip in unnoticed.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = SRC / "aje"

LAYERS = ["domain", "application", "infrastructure", "interfaces"]
ROOT_MODULES = {"aje.config", "aje.container", "aje.main", "aje.migrate", "aje.__main__", "aje"}

# Allowed outward dependencies:
#   the autonomic runtime (and the presenters) still persist through the Pydantic rows and the
#   Store; the engine reads Settings. Next step: repository ports for the runtime's writes.
KNOWN_SEAMS = {
    ("aje.application.ports.repositories", "aje.infrastructure.persistence.db"),
    ("aje.application.runtime.data_parallel", "aje.infrastructure.persistence.db"),
    ("aje.application.runtime.engine", "aje.infrastructure.persistence.db"),
    ("aje.application.runtime.engine", "aje.config"),
    ("aje.application.runtime.events", "aje.infrastructure.persistence.db"),
    ("aje.application.runtime.group", "aje.infrastructure.persistence.db"),
    ("aje.application.runtime.job", "aje.infrastructure.persistence.db"),
    ("aje.application.runtime.scheduler", "aje.infrastructure.persistence.db"),
    ("aje.application.services.presenters", "aje.infrastructure.persistence.db"),
}


def module_name(path: pathlib.Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def is_source_module(module: str) -> bool:
    base = SRC.joinpath(*module.split("."))
    return base.with_suffix(".py").exists() or (base / "__init__.py").exists()


def imports(path: pathlib.Path) -> set[str]:
    """First-party modules imported by ``path`` (``from pkg import mod`` counts as ``pkg.mod``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                submodule = f"{node.module}.{alias.name}"
                found.add(submodule if is_source_module(submodule) else node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return {m for m in found if m == "aje" or m.startswith("aje.")}


def layer(module: str) -> str:
    if module == "aje":
        return "package"  # only __version__
    if module == "aje.config":
        return "config"  # environment settings: read by adapters and the composition root
    parts = module.split(".")
    if len(parts) > 1 and parts[1] in LAYERS:
        return parts[1]
    return "root"


RANK = {
    "package": 0,
    "domain": 0,
    "application": 1,
    "infrastructure": 2,
    "interfaces": 2,
    "config": 2,
    "root": 3,
}
SOURCES = sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def violations():
    out = set()
    for path in SOURCES:
        mod = module_name(path)
        for target in imports(path):
            src_layer, dst_layer = layer(mod), layer(target)
            outward = RANK[dst_layer] > RANK[src_layer]
            sideways = {src_layer, dst_layer} == {"infrastructure", "interfaces"}
            if outward or sideways:
                out.add((mod, target))
    return out


def test_layers_exist():
    for name in LAYERS:
        assert (PACKAGE / name / "__init__.py").exists()


def test_dependencies_point_inwards():
    unexpected = violations() - KNOWN_SEAMS
    assert not unexpected, "outward imports:\n" + "\n".join(f"  {a} -> {b}" for a, b in sorted(unexpected))


def test_known_seams_are_still_needed():
    stale = KNOWN_SEAMS - violations()
    assert not stale, "remove from KNOWN_SEAMS (fixed):\n" + "\n".join(
        f"  {a} -> {b}" for a, b in sorted(stale)
    )


@pytest.mark.parametrize("path", [p for p in SOURCES if layer(module_name(p)) == "domain"], ids=module_name)
def test_domain_is_framework_free(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    banned = {
        "fastapi",
        "asyncpg",
        "sqlalchemy",
        "aio_pika",
        "kubernetes_asyncio",
        "starlette",
        "sse_starlette",
    }
    used = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    used |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not used & banned


def test_worker_sdk_does_not_depend_on_the_engine():
    for path in (SRC / "aje_worker").rglob("*.py"):
        assert not imports(path), f"{path.name} imports the engine"
