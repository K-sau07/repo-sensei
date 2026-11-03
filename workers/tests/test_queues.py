"""The queue lists in the Makefile and the worker image match the queues tasks are routed to."""

from __future__ import annotations

import re
from pathlib import Path

from sensei_core.jobs import Queue

ROOT = Path(__file__).resolve().parents[2]
QUEUES = {queue.value for queue in Queue}


def test_dev_worker_consumes_every_queue() -> None:
    makefile = (ROOT / "Makefile").read_text()
    match = re.search(r"^WORKER_QUEUES := (.+)$", makefile, re.MULTILINE)
    assert match is not None
    assert set(match.group(1).split(",")) == QUEUES


def test_worker_image_defaults_to_every_queue() -> None:
    dockerfile = (ROOT / "infra" / "docker" / "worker.Dockerfile").read_text()
    match = re.search(r"SENSEI_WORKER_QUEUES=(\S+)", dockerfile)
    assert match is not None
    assert set(match.group(1).split(",")) == QUEUES


def test_messages_have_priorities_and_public_repositories_wait() -> None:
    # 10.1: a public repository's index stages (the free tier) go after everything else.
    from sensei_core.config import Settings  # noqa: PLC0415
    from sensei_core.jobs import DEFAULT_PRIORITY, PUBLIC_PRIORITY, celery_config  # noqa: PLC0415

    config = celery_config(Settings(env="test"))
    options = config["broker_transport_options"]
    assert options["queue_order_strategy"] == "priority"
    assert options["priority_steps"] == list(range(10))
    assert config["task_default_priority"] == DEFAULT_PRIORITY
    assert DEFAULT_PRIORITY < PUBLIC_PRIORITY <= 9  # Redis takes 0 first
