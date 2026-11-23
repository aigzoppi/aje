"""Map domain errors to HTTP responses (the only place that knows both)."""

from __future__ import annotations

import fastapi
from fastapi import responses
from fastapi import status

from aje.domain import dsl
from aje.domain import errors


def install_error_handlers(app: fastapi.FastAPI) -> None:
    """Maps domain errors to HTTP responses (404, 409, 400, 422) with one JSON error shape."""

    @app.exception_handler(dsl.ContractError)
    async def _contract(_: fastapi.Request, exc: dsl.ContractError):
        return responses.JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "invalid contract", "error": exc.to_dict()},
        )

    @app.exception_handler(errors.NotFoundError)
    async def _not_found(_: fastapi.Request, exc: errors.NotFoundError):
        return responses.JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    @app.exception_handler(errors.ConflictError)
    async def _conflict(_: fastapi.Request, exc: errors.ConflictError):
        return responses.JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    @app.exception_handler(ValueError)  # includes InvalidOperationError
    async def _invalid(_: fastapi.Request, exc: ValueError):
        return responses.JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})
