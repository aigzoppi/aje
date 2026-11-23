"""Safe evaluator for rule expressions (no Python eval)."""

from __future__ import annotations

from collections.abc import Mapping
import math
import operator
from typing import Any

import pydantic


class EvalError(Exception):
    pass


def _num(fn):
    def wrapper(*args):
        if any(a is None for a in args):
            return None
        return fn(*args)

    return wrapper


FUNCTIONS: dict[str, Any] = {
    "min": _num(min),
    "max": _num(max),
    "abs": _num(abs),
    "ceil": _num(lambda x: int(math.ceil(x))),
    "floor": _num(lambda x: int(math.floor(x))),
    "round": _num(lambda x, nd=0: round(x, int(nd)) if nd else int(round(x))),
    "int": _num(int),
    "float": _num(float),
    "sqrt": _num(math.sqrt),
    "len": _num(len),
    "str": lambda x: "null" if x is None else str(x),
    "coalesce": lambda *xs: next((x for x in xs if x is not None), None),
    "is_null": lambda x: x is None,
}

_CMP = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}
_BIN = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "%": operator.mod,
}


class Scope(pydantic.BaseModel):
    """Name resolution context.

    Bare names resolve to fields of ``fact`` (inside a pattern), then globals.
    ``$vars`` resolve to bindings.
    """

    globals: Mapping[str, Any] = pydantic.Field(default_factory=dict)
    bindings: Mapping[str, Any] = pydantic.Field(default_factory=dict)
    fact: Mapping[str, Any] | None = None


def _get_attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        if name in obj:
            return obj[name]
        raise EvalError(f"no field {name!r}")
    raise EvalError(f"cannot read field {name!r} of {type(obj).__name__}")


def evaluate(expr: tuple, scope: Scope) -> Any:
    """Evaluates an expression tree.

    Missing values propagate as None (null): comparisons with null are false and
    arithmetic on null, or division by zero, yields null instead of raising.

    Args:
        expr: The expression tree, as produced by the parser.
        scope: Globals, rule bindings and the current fact.

    Returns:
        The value of the expression.

    Raises:
        EvalError: Unknown names, unbound variables, missing fields or bad operands.
    """
    kind = expr[0]
    if kind == "lit":
        return expr[1]
    if kind == "name":
        n = expr[1]
        if scope.fact is not None and n in scope.fact:
            return scope.fact[n]
        if n in scope.globals:
            return scope.globals[n]
        raise EvalError(f"unknown name {n!r}")
    if kind == "var":
        if expr[1] in scope.bindings:
            return scope.bindings[expr[1]]
        raise EvalError(f"unbound variable {expr[1]}")
    if kind == "attr":
        return _get_attr(evaluate(expr[1], scope), expr[2])
    if kind == "and":
        return bool(evaluate(expr[1], scope)) and bool(evaluate(expr[2], scope))
    if kind == "or":
        return bool(evaluate(expr[1], scope)) or bool(evaluate(expr[2], scope))
    if kind == "not":
        return not bool(evaluate(expr[1], scope))
    if kind == "neg":
        v = evaluate(expr[1], scope)
        return None if v is None else -v
    if kind == "cmp":
        a, b = evaluate(expr[2], scope), evaluate(expr[3], scope)
        op = expr[1]
        if a is None or b is None:
            # null-safe semantics: only equality tests are meaningful
            if op == "==":
                return a is b
            if op == "!=":
                return a is not b
            return False
        try:
            return _CMP[op](a, b)
        except TypeError as e:
            raise EvalError(str(e)) from None
    if kind == "bin":
        a, b = evaluate(expr[2], scope), evaluate(expr[3], scope)
        if a is None or b is None:
            return None
        op = expr[1]
        if op == "+" and (isinstance(a, str) or isinstance(b, str)):
            return f"{a}{b}"
        try:
            return _BIN[op](a, b)
        except ZeroDivisionError:
            return None
        except TypeError as e:
            raise EvalError(str(e)) from None
    if kind == "call":
        fn = FUNCTIONS.get(expr[1])
        if fn is None:
            raise EvalError(f"unknown function {expr[1]!r}")
        args = [evaluate(a, scope) for a in expr[2]]
        try:
            return fn(*args)
        except (TypeError, ValueError) as e:
            raise EvalError(f"{expr[1]}(): {e}") from None
    raise EvalError(f"bad expression node {kind!r}")


def walk(expr: tuple):
    """Yield every node of an expression tree."""
    yield expr
    kind = expr[0]
    if kind in ("attr", "neg", "not"):
        yield from walk(expr[1])
    elif kind in ("and", "or"):
        yield from walk(expr[1])
        yield from walk(expr[2])
    elif kind in ("cmp", "bin"):
        yield from walk(expr[2])
        yield from walk(expr[3])
    elif kind == "call":
        for a in expr[2]:
            yield from walk(a)
