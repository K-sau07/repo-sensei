"""Request/job context carried through logs, traces and database sessions.

Values are stored in ``contextvars`` (via structlog), so they follow async tasks automatically and
appear on every log line emitted inside the context.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import structlog

CONTEXT_KEYS = ("request_id", "org_id", "user_id", "repo_id", "job_id", "run_id")


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
