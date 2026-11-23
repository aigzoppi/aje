"""Contract use cases: discover catalogs and built-ins, validate without submitting."""

from __future__ import annotations

from typing import Any

from aje.application import schemas
from aje.application.runtime import compiler
from aje.application.services import presenters
from aje.domain import contracts
from aje.domain import dsl
from aje.domain import errors


class ContractService:
    def builtins(self) -> Any:
        return contracts.list_builtins()

    def catalog(self, scope: str) -> dict[str, Any]:
        if scope not in dsl.CATALOGS:
            raise errors.NotFoundError("scopes: " + ", ".join(dsl.CATALOGS))
        return dsl.CATALOGS[scope].describe()

    def validate(self, body: schemas.ContractValidateIn) -> dict[str, Any]:
        _, info = compiler.compile_contract(body.scope, body.contract)
        return {"valid": True, **presenters.contract_view(info)}
