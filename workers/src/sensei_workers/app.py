"""The Celery application: ``celery -A sensei_workers.app worker`` (see ``make worker``)."""

from __future__ import annotations

import os
import sys
from typing import Any

from celery import signals
from opentelemetry.instrumentation.celery import CeleryInstrumentor

from sensei_core.config import get_settings
from sensei_core.jobs import make_celery
from sensei_core.logging import configure_logging, get_logger
from sensei_core.telemetry import init_telemetry
from sensei_workers.beat import BEAT_SCHEDULE
from sensei_workers.runtime import set_runtime

# On macOS the prefork pool spawns its processes instead of forking them (billiard 4.3), and
# Celery only prepares its task registry in a spawned process when this flag says so. Set before
# the pool starts so every pool process inherits it. Linux forks and doesn't need it.
if sys.platform == "darwin":
    os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")

SERVICE_NAME = "sensei-worker"
TASK_MODULES = [
    "sensei_workers.tasks.webhooks",
    "sensei_workers.tasks.repos",
    "sensei_workers.tasks.ingest",
    "sensei_workers.tasks.discover",
    "sensei_workers.tasks.merkle",
]

settings = get_settings()
celery_app = make_celery(settings, main=SERVICE_NAME)
celery_app.conf.update(include=TASK_MODULES, beat_schedule=BEAT_SCHEDULE)

log = get_logger(__name__)
_instrumented = False


@signals.setup_logging.connect
def _setup_logging(**_: Any) -> None:
    """Connecting here stops Celery from installing its own handlers."""
    configure_logging(settings)


def _instrument() -> None:
    global _instrumented  # noqa: PLW0603 - once per process
    if _instrumented:
        return
    init_telemetry(settings, SERVICE_NAME)
    CeleryInstrumentor().instrument()
    _instrumented = True


@signals.worker_process_init.connect
def _process_init(**_: Any) -> None:
    """Each pool process sets up its own telemetry (exporters don't survive a fork)."""
    _instrument()


@signals.worker_init.connect
def _worker_init(sender: Any = None, **_: Any) -> None:
    # The solo and thread pools run tasks in this process; prefork children call _process_init.
    pool = getattr(getattr(sender, "pool_cls", None), "__module__", "")
    if not pool.endswith("prefork"):
        _instrument()


@signals.worker_process_shutdown.connect
def _process_shutdown(**_: Any) -> None:
    set_runtime(None)
