"""Domain errors. The application layer raises these; the API layer maps them to HTTP codes."""

from __future__ import annotations


class DomainError(Exception):
    """Base class of every error the engine reports to clients."""


class NotFoundError(DomainError, LookupError):
    """The requested job, group, schedule, task... does not exist."""


class ConflictError(DomainError):
    """The operation is not allowed in the current state (e.g. resuming a running job)."""


class InvalidOperationError(DomainError, ValueError):
    """The request is well-formed but does not apply (e.g. batches on a farm job)."""
