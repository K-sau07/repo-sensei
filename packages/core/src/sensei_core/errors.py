"""Domain errors raised by services. The API turns them into its JSON error envelope."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    status_code: int = 400
    code: str = "bad_request"

    def __init__(
        self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.details = details


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ForbiddenError(DomainError):
    status_code = 403
    code = "forbidden"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class InvalidError(DomainError):
    status_code = 422
    code = "validation_failed"
