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
