"""Structured logging for every repo-sensei process.

* JSON lines (orjson) outside dev, readable console output in dev.
* Context values (request id, org, user, repo, job) are merged into every event.
* Secrets are redacted before anything is rendered.
* Standard-library loggers (uvicorn, celery, sqlalchemy ...) go through the same pipeline.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import orjson
import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from sensei_core.config import Settings, get_settings
from sensei_core.redaction import redact_event


def _redaction_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    return redact_event(event_dict)


def _orjson_dumps(obj: MutableMapping[str, Any], **_kwargs: Any) -> str:
    return orjson.dumps(obj, default=str).decode()


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redaction_processor,
    ]


def configure_logging(settings: Settings | None = None, *, force_json: bool | None = None) -> None:
    """Configure structlog and the stdlib root logger. Safe to call more than once."""
    settings = settings or get_settings()
    use_json = settings.use_json_logs if force_json is None else force_json

    renderer: Processor
    if use_json:
        renderer = structlog.processors.JSONRenderer(serializer=_orjson_dumps)
        exception_processor: Processor = structlog.processors.dict_tracebacks
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        exception_processor = structlog.processors.format_exc_info

    shared = _shared_processors()
    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            exception_processor,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
