"""AST of the contract rule language.

Expressions are plain tuples so they are cheap to evaluate:
    ("lit", value) | ("var", "$x") | ("name", "x") | ("attr", expr, "field")
    ("call", "fn", [expr...]) | ("neg", expr) | ("not", expr)
    ("and", a, b) | ("or", a, b) | ("cmp", op, a, b) | ("bin", op, a, b)
"""

from __future__ import annotations

from typing import Any

import pydantic

Expr = tuple


class FieldBinding(pydantic.BaseModel):
    var: str
    field: str


class Test(pydantic.BaseModel):
    expr: Expr


Constraint = FieldBinding | Test


class Pattern(pydantic.BaseModel):
    fact_type: str
    binding: str | None = None
    constraints: list[Constraint] = pydantic.Field(default_factory=list)


class Condition(pydantic.BaseModel):
    kind: str  # "pattern" | "not" | "exists" | "eval"
    pattern: Pattern | None = None
    expr: Expr | None = None


class Action(pydantic.BaseModel):
    name: str
    args: list[Expr]
    line: int = 0


class Rule(pydantic.BaseModel):
    name: str
    conditions: list[Condition]
    actions: list[Action]
    salience: int = 0
    no_loop: bool = True
    cooldown: float = 0.0
    activation_group: str | None = None
    enabled: bool = True
    line: int = 0
    order: int = 0


class Contract(pydantic.BaseModel):
    package: str | None
    globals: dict[str, Any]
    global_types: dict[str, str | None]
    rules: list[Rule]
    source: str = ""
