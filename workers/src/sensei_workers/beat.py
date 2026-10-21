"""Periodic tasks (``make beat`` runs the scheduler)."""

from __future__ import annotations

from typing import Any

from sensei_core.jobs import TaskName

BEAT_SCHEDULE: dict[str, dict[str, Any]] = {
    # Deliveries stored while the queue was unreachable still get processed.
    "sweep-webhooks": {"task": TaskName.SWEEP_WEBHOOKS.value, "schedule": 300.0},
    # The working-copy cache stays within its disk budget.
    "evict-repo-cache": {"task": TaskName.EVICT_REPO_CACHE.value, "schedule": 3600.0},
    # An index job whose worker vanished without a word doesn't block its repository forever.
    "sweep-stalled-jobs": {"task": TaskName.SWEEP_STALLED_JOBS.value, "schedule": 600.0},
}
