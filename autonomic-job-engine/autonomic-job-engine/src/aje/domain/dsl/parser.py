"""Parse contract source text into an AST."""

from __future__ import annotations

import functools
import json
import pathlib
import re
from typing import Any

import lark
from lark import exceptions

from aje.domain.dsl import ast
from aje.domain.dsl import evaluator


class ContractError(ValueError):
    """Raised for syntax or semantic errors in a contract."""

    def __init__(self, message: str, line: int | None = None, column: int | None = None):
        self.message = message
        self.line = line
        self.column = column
        where = ""
        if line:
            where = f" (line {line}, column {column})" if column else f" (line {line})"
        super().__init__(f"{message}{where}")

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "line": self.line, "column": self.column}


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


def parse_duration(text: str) -> float:
    m = _DURATION.match(text)
    if not m:
        raise ContractError(f'invalid duration {text!r} (use e.g. "500ms", "30s", "2m")')
    return float(m.group(1)) * _UNITS[m.group(2)]


def _unquote(tok: str) -> str:
    return json.loads(tok)


@lark.v_args(inline=True)
class _ToAst(lark.Transformer):
    """Turns the lark parse tree into contract AST nodes (one method per grammar rule)."""

    # ---------- expressions ----------
    def number(self, tok):
        text = str(tok)
        return ("lit", float(text) if any(c in text for c in ".eE") else int(text))

    def string(self, tok):
        return ("lit", _unquote(str(tok)))

    def true(self, *_):
        return ("lit", True)

    def false(self, *_):
        return ("lit", False)

    def null(self, *_):
        return ("lit", None)

    def var(self, tok):
        return ("var", str(tok))

    def name(self, tok):
        return ("name", str(tok))

    def attr(self, obj, tok):
        return ("attr", obj, str(tok))

    def call(self, fname, *args):
        return ("call", str(fname), [a for a in args if a is not None])

    def neg(self, sign, e):
        del sign  # Unused: always "-".
        if e[0] == "lit" and isinstance(e[1], (int, float)) and not isinstance(e[1], bool):
            return ("lit", -e[1])
        return ("neg", e)

    def not_(self, e):
        return ("not", e)

    def and_(self, a, b):
        return ("and", a, b)

    def or_(self, a, b):
        return ("or", a, b)

    def cmp(self, a, op, b):
        return ("cmp", str(op), a, b)

    def bin(self, a, op, b):
        return ("bin", str(op), a, b)

    # ---------- conditions ----------
    def field_binding(self, var, fld):
        return ast.FieldBinding(var=str(var), field=str(fld))

    def test(self, e):
        return ast.Test(expr=e)

    def pattern(self, binding, fact_type, *constraints):
        return ast.Pattern(
            fact_type=str(fact_type),
            binding=str(binding) if binding is not None else None,
            constraints=[c for c in constraints if c is not None],
        )

    def condition(self, p):
        return ast.Condition(kind="pattern", pattern=p)

    def not_ce(self, p):
        return ast.Condition(kind="not", pattern=p)

    def exists_ce(self, p):
        return ast.Condition(kind="exists", pattern=p)

    def eval_ce(self, e):
        return ast.Condition(kind="eval", expr=e)

    # ---------- actions ----------
    @lark.v_args(inline=False, meta=True)
    def action(self, meta, children):
        name, *args = children
        return ast.Action(
            name=str(name), args=[a for a in args if a is not None], line=getattr(meta, "line", 0)
        )

    # ---------- rule attributes ----------
    def salience(self, *parts):
        sign = -1 if len(parts) == 2 else 1
        return ("salience", sign * int(float(str(parts[-1]))))

    def no_loop(self, keyword, value=None):
        del keyword  # Unused.
        return ("no_loop", True if value is None else value[1])

    def cooldown(self, tok):
        return ("cooldown", parse_duration(_unquote(str(tok))))

    def activation_group(self, keyword, tok):
        del keyword  # Unused.
        return ("activation_group", _unquote(str(tok)))

    def enabled(self, value):
        return ("enabled", value[1])

    # ---------- top level ----------
    @lark.v_args(inline=False, meta=True)
    def rule(self, meta, children):
        """Builds a Rule from its name, attributes (pairs), conditions and actions."""
        name = _unquote(str(children[0]))
        attrs: dict[str, Any] = {}
        conditions: list[ast.Condition] = []
        actions: list[ast.Action] = []
        for c in children[1:]:
            if (
                isinstance(c, tuple)
                and len(c) == 2
                and isinstance(c[0], str)
                and not isinstance(c, lark.Token)
            ):
                attrs[c[0]] = c[1]
            elif isinstance(c, ast.Condition):
                conditions.append(c)
            elif isinstance(c, ast.Action):
                actions.append(c)
        return ast.Rule(
            name=name, conditions=conditions, actions=actions, line=getattr(meta, "line", 0), **attrs
        )

    def qname(self, *parts):
        return ".".join(str(p) for p in parts)

    def package_decl(self, q):
        return ("package", q)

    def import_decl(self, q):
        return ("import", q)

    def global_decl(self, *parts):
        if len(parts) == 3:
            gtype, gname, expr = parts
            return ("global", str(gtype), str(gname), expr)
        gname, expr = parts
        return ("global", None, str(gname), expr)

    @lark.v_args(inline=False)
    def start(self, items):
        return items


@functools.lru_cache(maxsize=1)
def _parser() -> lark.Lark:
    grammar = (pathlib.Path(__file__).parent / "grammar.lark").read_text()
    return lark.Lark(grammar, parser="lalr", propagate_positions=True, maybe_placeholders=True)


def parse_contract(source: str) -> ast.Contract:
    """Parses contract text. Globals must be constant expressions.

    Args:
        source: The contract source (DRL-like syntax).

    Returns:
        The parsed contract, with global values already evaluated.

    Raises:
        ContractError: Syntax errors, duplicate rule names, or invalid globals.
    """
    try:
        tree = _parser().parse(source)
        items = _ToAst().transform(tree)
    except exceptions.UnexpectedInput as e:
        expected = sorted(getattr(e, "expected", []) or getattr(e, "allowed", []) or [])
        hint = "; expected one of: " + ", ".join(expected[:8]) if expected else ""
        token = getattr(e, "token", None)
        got = f" near {str(token)!r}" if token is not None else ""
        raise ContractError(f"syntax error{got}{hint}", e.line, e.column) from None
    except exceptions.VisitError as e:
        if isinstance(e.orig_exc, ContractError):
            raise e.orig_exc from None
        raise ContractError(str(e.orig_exc)) from None

    package = None
    globals_: dict[str, Any] = {}
    gtypes: dict[str, Any] = {}
    rules: list[ast.Rule] = []
    names: set[str] = set()
    for item in items:
        if isinstance(item, ast.Rule):
            if item.name in names:
                raise ContractError(f"duplicate rule name {item.name!r}", item.line)
            names.add(item.name)
            item.order = len(rules)
            rules.append(item)
        elif item[0] == "package":
            package = item[1]
        elif item[0] == "global":
            _, gtype, gname, expr = item
            try:
                value = evaluator.evaluate(expr, evaluator.Scope(globals=globals_))
            except evaluator.EvalError as e:
                raise ContractError(f"global {gname!r} must be a constant expression: {e}") from None
            globals_[gname] = _coerce(value, gtype, gname)
            gtypes[gname] = gtype
    return ast.Contract(package=package, globals=globals_, global_types=gtypes, rules=rules, source=source)


_TYPES = {
    "int": int,
    "long": int,
    "integer": int,
    "float": float,
    "double": float,
    "number": float,
    "String": str,
    "string": str,
    "str": str,
    "boolean": bool,
    "bool": bool,
}


def _coerce(value: Any, gtype: str | None, gname: str) -> Any:
    if gtype is None or value is None:
        return value
    target = _TYPES.get(gtype)
    if target is None:
        raise ContractError(f"unknown type {gtype!r} for global {gname!r}")
    if target is bool and not isinstance(value, bool):
        raise ContractError(f"global {gname!r} declared boolean but got {value!r}")
    try:
        return target(value)
    except (TypeError, ValueError):
        raise ContractError(f"global {gname!r} declared {gtype} but got {value!r}") from None
