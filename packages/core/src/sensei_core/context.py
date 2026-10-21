"""Request/job context carried through logs, traces and database sessions.

Values are stored in ``contextvars`` (via structlog), so they follow async tasks automatically and
appear on every log line emitted inside the context.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

CONTEXT_KEYS = (
    "request_id",
    "org_id",
    "user_id",
    "repo_id",
    "job_id",
    "run_id",
    "task",
    "task_id",
)

# Kept out of the log context on purpose (IP addresses are personal data); read by the audit log.
_client_ip: ContextVar[str | None] = ContextVar("client_ip", default=None)


@contextmanager
def bind_context(**values: Any) -> Generator[None]:
    """Bind context values for the duration of the ``with`` block, then restore."""
    unknown = set(values) - set(CONTEXT_KEYS)
    if unknown:
        msg = f"unknown context keys: {sorted(unknown)}"
        raise ValueError(msg)
    cleaned = {k: str(v) for k, v in values.items() if v is not None}
    with structlog.contextvars.bound_contextvars(**cleaned):
        yield


def current_context() -> dict[str, Any]:
    """Return the currently bound context values."""
    return dict(structlog.contextvars.get_contextvars())


@contextmanager
def bind_client_ip(ip: str | None) -> Generator[None]:
    """Remember the caller's IP address for the duration of a request."""
    token = _client_ip.set(ip)
    try:
        yield
    finally:
        _client_ip.reset(token)


def current_client_ip() -> str | None:
    return _client_ip.get()
