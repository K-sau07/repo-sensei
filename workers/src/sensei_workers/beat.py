"""Periodic tasks (``make beat`` runs the scheduler)."""

from __future__ import annotations

from typing import Any

from sensei_core.jobs import TaskName

BEAT_SCHEDULE: dict[str, dict[str, Any]] = {
    # Deliveries stored while the queue was unreachable still get processed.
    "sweep-webhooks": {"task": TaskName.SWEEP_WEBHOOKS.value, "schedule": 300.0},
}
