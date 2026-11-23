"""Google Python Style Guide rules that neither ruff nor pylint checks.

* 2.2   import packages and modules only, never individual classes or functions
        (exempt: typing, collections.abc, typing_extensions, and the names a package's
        ``__init__.py`` re-exports in ``__all__``)
* 2.17  no ``@staticmethod``: write a module-level function instead

Formatting, naming, docstrings and the other rules are checked by ``ruff format``,
``ruff check`` and ``pylint --rcfile pylintrc`` (see pyproject.toml).
"""

import ast
import importlib
import inspect
import pathlib
import warnings

SRC = pathlib.Path(__file__).resolve().parents[2]
SOURCES = sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)
EXEMPT_MODULES = frozenset({"__future__", "typing", "collections.abc", "typing_extensions"})


def relative(path: pathlib.Path) -> str:
    return path.relative_to(SRC).as_posix()


def reexported(tree: ast.Module) -> set[str]:
    """Names listed in a module's ``__all__``."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return set()


def is_module(package: str, name: str) -> bool:
    with warnings.catch_warnings():  # probing also imports deprecated fallback modules
        warnings.simplefilter("ignore")
        return _is_module(package, name)


def _is_module(package: str, name: str) -> bool:
    parent = importlib.import_module(package)
    attr = getattr(parent, name, None)
    if attr is None:
        try:
            importlib.import_module(f"{package}.{name}")
        except ImportError:
            return False
        return True
    return inspect.ismodule(attr)


def member_imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed = reexported(tree) if path.name == "__init__.py" else set()
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module in EXEMPT_MODULES or node.level:
            continue  # relative imports are rejected by ruff (TID252)
        for alias in node.names:
            if alias.name not in allowed and not is_module(node.module, alias.name):
                found.append(f"{relative(path)}:{node.lineno}: from {node.module} import {alias.name}")
    return found


def static_methods(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{relative(path)}:{node.lineno}: {node.name}"
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in node.decorator_list)
    ]


def test_imports_are_modules_not_names():
    found = [v for path in SOURCES for v in member_imports(path)]
    assert not found, "import the module instead (styleguide 2.2):\n" + "\n".join(found)


def test_no_static_methods():
    found = [v for path in SOURCES for v in static_methods(path)]
    assert not found, "use a module-level function (styleguide 2.17):\n" + "\n".join(found)
