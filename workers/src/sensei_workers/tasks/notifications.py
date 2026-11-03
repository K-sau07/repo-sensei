"""Notifications' emails and Slack posts (8.10): what's pending, sent, every minute (beat). See
:mod:`sensei_core.notifications.deliver`."""

from __future__ import annotations

from sensei_core.jobs import TaskName
from sensei_core.notifications.deliver import Senders, deliver_pending
from sensei_core.notifications.senders import HttpSlackPoster, emailer
from sensei_workers.app import celery_app
from sensei_workers.base import SenseiTask
from sensei_workers.runtime import get_runtime


@celery_app.task(bind=True, base=SenseiTask, name=TaskName.DELIVER_NOTIFICATIONS.value, system=True)
def deliver_notifications(self: SenseiTask) -> int:
    """Send what's pending (every org's: the system database). Returns how many were sent."""
    runtime = get_runtime()
    settings = runtime.settings
    http = runtime.notify_http
    senders = Senders(
        emailer=emailer(http, settings),
        slack=HttpSlackPoster(http),
        cipher=runtime.cipher,
        origin=settings.web.origin,
        max_attempts=settings.notify.max_attempts,
    )
    done = runtime.run(deliver_pending(self.system_session, senders))
    return done.sent
