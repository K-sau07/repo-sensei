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
    # A fix whose stage lost its worker fails, saying so; a lost queued stage is sent again.
    "sweep-stalled-fixes": {"task": TaskName.SWEEP_STALLED_FIXES.value, "schedule": 600.0},
    # Snapshots past their retention (5.2.5) go, with their archives and artifacts.
    "sweep-snapshots": {"task": TaskName.SWEEP_SNAPSHOTS.value, "schedule": 86400.0},
    # Expired OAuth requests, device codes and tokens (7.2) don't pile up.
    "prune-oauth": {"task": TaskName.PRUNE_OAUTH.value, "schedule": 86400.0},
    # Web vitals (10.11) are kept as long as insights look back.
    "prune-vitals": {"task": TaskName.PRUNE_VITALS.value, "schedule": 86400.0},
    # Orgs deleted more than their grace period ago (10.7.3) go, with everything in them.
    "purge-deleted-orgs": {"task": TaskName.PURGE_DELETED_ORGS.value, "schedule": 86400.0},
    # Notifications' emails and Slack posts (8.10) go out within a minute; a round not taken
    # in time is dropped (the next one sends what it would have).
    "deliver-notifications": {
        "task": TaskName.DELIVER_NOTIFICATIONS.value,
        "schedule": 60.0,
        "options": {"expires": 55},
    },
    # Every org's spend today against the global cap (10.1): past it, read-only until midnight.
    "check-global-spend": {
        "task": TaskName.CHECK_GLOBAL_SPEND.value,
        "schedule": 60.0,
        "options": {"expires": 55},
    },
}
