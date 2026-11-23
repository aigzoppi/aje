"""HTTP adapter for contract discovery and validation."""

from __future__ import annotations

import fastapi

from aje.application import schemas
from aje.interfaces.http import deps

router = fastapi.APIRouter(prefix="/v1/contracts", tags=["contracts"])


@router.get("/builtin", summary="Built-in contracts (pre-defined management plans)")
async def builtins(contracts: deps.ContractServiceDep):
    return contracts.builtins()


@router.get("/catalog/{scope}", summary="Facts, fields and actions a contract may use in a scope")
async def catalog(scope: str, contracts: deps.ContractServiceDep):
    return contracts.catalog(scope)


@router.post("/validate", summary="Parse and type-check a contract without submitting anything")
async def validate(body: schemas.ContractValidateIn, contracts: deps.ContractServiceDep):
    return contracts.validate(body)
