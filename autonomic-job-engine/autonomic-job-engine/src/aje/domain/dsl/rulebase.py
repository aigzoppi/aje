"""Compile a contract against a catalog and run it as a forward-chaining agenda.

Semantics (one evaluation per manager tick):
  * working memory = facts gathered by the monitor phase (lists of dicts per type)
  * every rule is matched against working memory; each consistent set of
    bindings produces an activation (Drools-style joins, ``not``, ``exists``, ``eval``)
  * the agenda is ordered by salience (high first), then by rule order
  * ``activation-group``: once one rule of a group fires, the others are skipped
  * ``cooldown``: a rule that fired is not eligible again until the cooldown expires
  * ``no-loop`` is implicit: facts are a snapshot, so actions never retrigger the
    same tick; effects are observed at the next tick
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
import time
from typing import Any

import pydantic

from aje.domain.dsl import ast
from aje.domain.dsl import catalog as catalog_lib
from aje.domain.dsl import evaluator
from aje.domain.dsl import parser


class ActionCall(pydantic.BaseModel):
    """An action a fired rule asks for, with its arguments already evaluated."""

    name: str
    args: list[Any]
    rule: str


class Firing(pydantic.BaseModel):
    """One rule that fired: its bindings and the action calls it produced (or the error)."""

    rule: str
    salience: int
    bindings: dict[str, Any]
    calls: list[ActionCall] = pydantic.Field(default_factory=list)
    error: str | None = None


class Activation(pydantic.BaseModel):
    """A rule whose conditions matched, with the bindings of that match."""

    rule: ast.Rule
    bindings: dict[str, Any]


class RuleBase:
    """A contract compiled against a catalog, ready to run over working memory.

    Attributes:
        contract: The parsed contract.
        catalog: Fact types and actions the contract may use.
        globals: Global values: the contract's declarations with overrides applied.
        last_fired: Rule name -> time it last fired (drives ``cooldown``).
        eval_errors: Expression errors seen during the last ``run`` (the expression was false).
    """

    def __init__(
        self, contract: ast.Contract, catalog: catalog_lib.Catalog, overrides: dict[str, Any] | None = None
    ):
        self.contract = contract
        self.catalog = catalog
        self.globals = dict(contract.globals)
        for key, value in (overrides or {}).items():
            if key not in self.globals:
                declared = _listing(sorted(self.globals)) or "none"
                raise parser.ContractError(
                    f"unknown contract parameter {key!r}; declared globals: {declared}"
                )
            self.globals[key] = value
        _validate(contract, catalog, set(self.globals))
        self.last_fired: dict[str, float] = {}
        self.eval_errors: list[str] = []

    @classmethod
    def compile(
        cls, source: str, catalog: catalog_lib.Catalog, overrides: dict[str, Any] | None = None
    ) -> RuleBase:
        return cls(parser.parse_contract(source), catalog, overrides)

    @property
    def rules(self) -> list[ast.Rule]:
        return self.contract.rules

    # ------------------------------------------------------------------ match
    def _match(self, conds: list[ast.Condition], i: int, bindings: dict, facts: dict) -> Iterator[dict]:
        """Yields every set of bindings that satisfies ``conds[i:]``.

        Args:
            conds: The rule's conditions, in order.
            i: Index of the first condition still to match.
            bindings: Variables bound by the conditions before ``i``.
            facts: Working memory: fact type -> list of facts.

        Yields:
            The bindings extended by one complete match of the remaining conditions.
        """
        if i == len(conds):
            yield bindings
            return
        c = conds[i]
        if c.kind == "eval":
            if self._truthy(c.expr, evaluator.Scope(globals=self.globals, bindings=bindings)):
                yield from self._match(conds, i + 1, bindings, facts)
            return
        matches = self._match_pattern(c.pattern, bindings, facts)
        if c.kind == "pattern":
            for b in matches:
                yield from self._match(conds, i + 1, b, facts)
        elif c.kind == "not":
            if next(matches, None) is None:
                yield from self._match(conds, i + 1, bindings, facts)
        elif c.kind == "exists":
            if next(matches, None) is not None:
                yield from self._match(conds, i + 1, bindings, facts)

    def _match_pattern(self, p: ast.Pattern, bindings: dict, facts: dict) -> Iterator[dict]:
        """Yields ``bindings`` extended by each fact of ``p.fact_type`` that satisfies ``p``.

        Args:
            p: The pattern: a fact type, its constraints and an optional binding.
            bindings: Variables bound so far.
            facts: Working memory: fact type -> list of facts.

        Yields:
            A copy of ``bindings`` with the pattern's field bindings (and the fact itself,
            when the pattern binds it) for every matching fact.
        """
        for fact in facts.get(p.fact_type, ()):
            b = dict(bindings)
            ok = True
            for con in p.constraints:
                if isinstance(con, ast.FieldBinding):
                    b[con.var] = fact.get(con.field)
                elif not self._truthy(con.expr, evaluator.Scope(globals=self.globals, bindings=b, fact=fact)):
                    ok = False
                    break
            if ok:
                if p.binding:
                    b[p.binding] = fact
                yield b

    def _truthy(self, expr, scope: evaluator.Scope) -> bool:
        try:
            return bool(evaluator.evaluate(expr, scope))
        except evaluator.EvalError as e:
            self.eval_errors.append(str(e))
            return False

    # ------------------------------------------------------------------ run
    def agenda(self, facts: dict[str, list[dict]], now: float | None = None) -> list[Activation]:
        """Matches every eligible rule against ``facts``.

        Args:
            facts: Working memory: fact type -> list of facts.
            now: Current time (epoch seconds) for cooldowns; defaults to the clock.

        Returns:
            The activations, highest salience first, then in rule order. Disabled rules
            and rules still in their cooldown are left out.
        """
        now = time.time() if now is None else now
        acts: list[Activation] = []
        for rule in self.rules:
            if not rule.enabled:
                continue
            last = self.last_fired.get(rule.name)
            if rule.cooldown and last is not None and now - last < rule.cooldown:
                continue
            for b in self._match(rule.conditions, 0, {}, facts):
                acts.append(Activation(rule=rule, bindings=b))
        acts.sort(key=lambda a: (-a.rule.salience, a.rule.order))
        return acts

    def run(self, facts: dict[str, list[dict]], now: float | None = None) -> list[Firing]:
        """Evaluates the agenda and returns the firings with evaluated action calls.

        Args:
            facts: Working memory: fact type -> list of facts.
            now: Current time (epoch seconds) for cooldowns; defaults to the clock.

        Returns:
            One firing per activation that fired, in agenda order. Activation groups
            let only their first rule fire.
        """
        now = time.time() if now is None else now
        self.eval_errors = []
        fired_groups: dict[str, str] = {}
        firings: list[Firing] = []
        for act in self.agenda(facts, now):
            rule = act.rule
            grp = rule.activation_group
            if grp and grp in fired_groups and fired_groups[grp] != rule.name:
                continue
            firing = Firing(rule=rule.name, salience=rule.salience, bindings=_public(act.bindings))
            scope = evaluator.Scope(globals=self.globals, bindings=act.bindings)
            try:
                for a in rule.actions:
                    args = [evaluator.evaluate(x, scope) for x in a.args]
                    firing.calls.append(ActionCall(name=a.name, args=args, rule=rule.name))
            except evaluator.EvalError as e:
                firing.error = f"action evaluation failed: {e}"
                firing.calls = []
            firings.append(firing)
            self.last_fired[rule.name] = now
            if grp:
                fired_groups[grp] = rule.name
        return firings


def _public(bindings: dict) -> dict:
    out = {}
    for k, v in bindings.items():
        if isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in v.items() if kk in ("id", "name", "job_id", "reason")} or "<fact>"
        else:
            out[k] = v
    return out


def _listing(items: Iterable[str]) -> str:
    return ", ".join(items)


# ---------------------------------------------------------------------- validate
def _validate(contract: ast.Contract, catalog: catalog_lib.Catalog, global_names: set[str]) -> None:
    for rule in contract.rules:
        _validate_rule(rule, catalog, global_names)


def _validate_rule(rule: ast.Rule, catalog: catalog_lib.Catalog, global_names: set[str]) -> None:
    """Checks one rule against the catalog and the declared globals.

    Args:
        rule: The rule to check.
        catalog: Fact types (with their fields) and actions allowed in this scope.
        global_names: Names of the contract's globals.

    Raises:
        parser.ContractError: Unknown fact types, fields, names, functions or actions,
            variables used before binding or bound twice, or wrong action arity.
    """
    where = f"rule {rule.name!r}"
    var_types: dict[str, str | None] = {}
    for cond in rule.conditions:
        if cond.kind == "eval":
            _check_expr(cond.expr, None, var_types, rule, catalog, global_names)
            continue
        p = cond.pattern
        if p.fact_type not in catalog.facts:
            raise parser.ContractError(
                f"{where}: unknown fact type {p.fact_type!r} for {catalog.scope} contracts; "
                f"available: {_listing(catalog.facts)}",
                rule.line,
            )
        local = dict(var_types)
        for con in p.constraints:
            if isinstance(con, ast.FieldBinding):
                if con.field not in catalog.facts[p.fact_type]:
                    raise parser.ContractError(
                        f"{where}: {p.fact_type} has no field {con.field!r}", rule.line
                    )
                if con.var in local:
                    raise parser.ContractError(f"{where}: variable {con.var} bound twice", rule.line)
                local[con.var] = None
            else:
                _check_expr(con.expr, p.fact_type, local, rule, catalog, global_names)
        if p.binding:
            if p.binding in local:
                raise parser.ContractError(f"{where}: variable {p.binding} bound twice", rule.line)
            local[p.binding] = p.fact_type
        if cond.kind == "pattern":  # bindings inside not/exists stay local
            var_types = local

    for act in rule.actions:
        spec = catalog.actions.get(act.name)
        if spec is None:
            raise parser.ContractError(
                f"{where}: action {act.name}() is not allowed for {catalog.scope} contracts; "
                f"allowed: {_listing(sorted(catalog.actions))}",
                act.line or rule.line,
            )
        n = len(act.args)
        if n < spec.min_args or (spec.max_args is not None and n > spec.max_args):
            arity = str(spec.min_args)
            if spec.max_args != spec.min_args:
                arity += ".." + str(spec.max_args or "n")
            raise parser.ContractError(
                f"{where}: {act.name}() takes {arity} argument(s), got {n}", act.line or rule.line
            )
        for a in act.args:
            _check_expr(a, None, var_types, rule, catalog, global_names)


def _check_expr(
    expr,
    fact_type: str | None,
    local: dict[str, str | None],
    rule: ast.Rule,
    catalog: catalog_lib.Catalog,
    global_names: set[str],
) -> None:
    """Checks the names, variables, fields and functions an expression uses.

    Args:
        expr: The expression tree.
        fact_type: The fact type whose fields are in scope (inside a pattern), or None.
        local: Variables bound so far -> the fact type they hold (None for a field value).
        rule: The rule the expression belongs to (for error messages).
        catalog: Fact types and their fields.
        global_names: Names of the contract's globals.

    Raises:
        parser.ContractError: The expression refers to something that does not exist.
    """
    where = f"rule {rule.name!r}"
    for node in evaluator.walk(expr):
        kind = node[0]
        if kind == "name":
            n = node[1]
            in_fact = fact_type is not None and n in catalog.facts[fact_type]
            if not in_fact and n not in global_names:
                hint = f"; fields of {fact_type}: {_listing(catalog.facts[fact_type])}" if fact_type else ""
                raise parser.ContractError(
                    f"{where}: unknown name {n!r} (not a field or a global){hint}", rule.line
                )
        elif kind == "var":
            if node[1] not in local:
                raise parser.ContractError(f"{where}: variable {node[1]} used before it is bound", rule.line)
        elif kind == "attr" and node[1][0] == "var":
            t = local.get(node[1][1])
            if t is not None and node[2] not in catalog.facts[t]:
                raise parser.ContractError(
                    f"{where}: {t} has no field {node[2]!r}; fields: {_listing(catalog.facts[t])}", rule.line
                )
        elif kind == "call" and node[1] not in evaluator.FUNCTIONS:
            raise parser.ContractError(
                f"{where}: unknown function {node[1]!r}; available: {_listing(sorted(evaluator.FUNCTIONS))}",
                rule.line,
            )
