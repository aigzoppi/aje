"""Contract compilation shared by the engine, the scheduler and the services."""

from __future__ import annotations

from aje.application import schemas
from aje.domain import contracts
from aje.domain import dsl


def compile_contract(scope: str, spec: schemas.ContractSpec | None) -> tuple[dsl.RuleBase | None, dict]:
    """Validates a contract for a scope.

    Args:
        scope: ``farm``, ``data_parallel`` or ``group``.
        spec: A built-in name or a source, plus global overrides; None for the scope's default.

    Returns:
        The compiled rule base and the description stored with the job or group.

    Raises:
        dsl.ContractError: Unknown built-in, built-in of another scope, or an invalid contract.
    """
    catalog = dsl.CATALOGS[scope]
    spec = spec or schemas.ContractSpec()
    if spec.source:
        source, name = spec.source, None
    else:
        name = spec.builtin or contracts.DEFAULTS[scope]
        try:
            source = contracts.builtin_source(name)
        except KeyError as e:
            raise dsl.ContractError(str(e.args[0])) from None
        if contracts.builtin_scope(name) != scope:
            raise dsl.ContractError(
                f"built-in contract {name!r} is for {contracts.builtin_scope(name)} scope, not {scope}"
            )
    rulebase = dsl.RuleBase.compile(source, catalog, spec.globals)
    info = {
        "builtin": name,
        "source": source,
        "globals": spec.globals,
        "rules": [r.name for r in rulebase.rules],
        "parameters": rulebase.globals,
    }
    return rulebase, info
