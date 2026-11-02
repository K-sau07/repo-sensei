"""What the CLI says when it can't go on: a message and, when there is one, what to do."""

from __future__ import annotations


class CliError(Exception):
    def __init__(self, message: str, *, hint: str | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code
