"""Background jobs, as seen by both sides: queue and task names, and the Celery configuration.

The API only *sends* tasks (by name, so it doesn't import worker code); the workers package
defines them. Both build their Celery app with :func:`make_celery`.
"""

from __future__ import annotations

import asyncio
import enum
import uuid
from typing import Any, Protocol

from celery import Celery
from kombu import Queue as KombuQueue

from sensei_core.config import Settings, with_redis_db
from sensei_core.logging import get_logger

log = get_logger(__name__)


class Queue(enum.StrEnum):
    DEFAULT = "default"
    WEBHOOKS = "webhooks"
    INGEST = "ingest"
    PARSE = "parse"
    EMBED = "embed"
    LLM = "llm"
    GRAPH = "graph"
    AGENT = "agent"
    SANDBOX = "sandbox"
    MAINTENANCE = "maintenance"


class TaskName(enum.StrEnum):
    PROCESS_GITHUB_WEBHOOK = "sensei.webhooks.process_github"
    SWEEP_WEBHOOKS = "sensei.webhooks.sweep"
    PURGE_REPO = "sensei.maintenance.purge_repo"
    CLONE_SNAPSHOT = "sensei.ingest.clone_snapshot"
    DISCOVER_FILES = "sensei.ingest.discover_files"
    BUILD_MERKLE = "sensei.ingest.build_merkle"
    EVICT_REPO_CACHE = "sensei.maintenance.evict_repo_cache"


# Tasks go to the queue named by the second part of their name ("sensei.<queue>.<task>").
TASK_ROUTES = {f"sensei.{queue.value}.*": {"queue": queue.value} for queue in Queue}


def broker_url(settings: Settings) -> str:
    if settings.worker.broker_url is not None:
        return settings.worker.broker_url.get_secret_value()
    return with_redis_db(settings.redis.url.get_secret_value(), 1)


def result_backend(settings: Settings) -> str:
    if settings.worker.result_backend is not None:
        return settings.worker.result_backend.get_secret_value()
    return with_redis_db(settings.redis.url.get_secret_value(), 2)


def celery_config(settings: Settings) -> dict[str, Any]:
    worker = settings.worker
    return {
        "broker_url": broker_url(settings),
        "result_backend": result_backend(settings),
        "result_expires": worker.result_expires_seconds,
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "timezone": "UTC",
        "enable_utc": True,
        # At-least-once: a task is acknowledged only after it finishes, and goes back to the
        # queue if the process running it dies. Tasks must therefore be idempotent.
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        "worker_prefetch_multiplier": 1,
        "task_soft_time_limit": worker.soft_time_limit_seconds,
        "task_time_limit": worker.time_limit_seconds,
        "broker_transport_options": {"visibility_timeout": worker.visibility_timeout_seconds},
        "broker_connection_retry_on_startup": True,
        "task_default_queue": Queue.DEFAULT.value,
        "task_queues": [KombuQueue(queue.value) for queue in Queue],
        "task_routes": TASK_ROUTES,
        # Eager tasks still retry in place; their errors end up on the result, as in a worker.
        "task_always_eager": worker.always_eager,
        # Our structlog setup owns logging.
        "worker_hijack_root_logger": False,
        "task_send_sent_event": False,
    }


def make_celery(settings: Settings, main: str = "sensei") -> Celery:
    app = Celery(main)
    app.conf.update(celery_config(settings))
    return app


class RepoJobs(Protocol):
    """Background work for repositories, requested by the API."""

    async def purge(self, repo_id: uuid.UUID) -> None: ...


class CeleryRepoJobs:
    def __init__(self, app: Celery) -> None:
        self._app = app

    async def purge(self, repo_id: uuid.UUID) -> None:
        # The repository is already hidden (soft-deleted); if the broker is down it stays that
        # way until purged later, so a failure here is logged, not raised.
        try:
            await asyncio.to_thread(
                self._app.send_task,
                TaskName.PURGE_REPO.value,
                kwargs={"repo_id": str(repo_id)},
                queue=Queue.MAINTENANCE.value,
            )
        except Exception:
            log.warning("could not queue repository purge", repo=str(repo_id), exc_info=True)


class CeleryDispatcher:
    """Sends stored webhook deliveries to the ``webhooks`` queue."""

    def __init__(self, app: Celery) -> None:
        self._app = app

    async def dispatch(self, delivery_id: uuid.UUID) -> None:
        try:
            # send_task blocks on the broker; keep it off the event loop.
            await asyncio.to_thread(
                self._app.send_task,
                TaskName.PROCESS_GITHUB_WEBHOOK.value,
                kwargs={"delivery_id": str(delivery_id)},
                queue=Queue.WEBHOOKS.value,
            )
        except Exception:
            log.warning(
                "could not queue webhook delivery", delivery=str(delivery_id), exc_info=True
            )
