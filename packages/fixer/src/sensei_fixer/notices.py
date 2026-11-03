"""What a fix's (or change's) moves tell people (8.10), from its state machine's events:

- ``awaiting_approval``: its plan is waiting: whoever asked for it and the org's admins (once
  per plan revision: an edited plan, planned again, asks again);
- ``done`` with a pull request: whoever asked for it;
- ``not_reproduced``, ``blocked``, ``failed``: whoever asked for it (once per stop: a retry that
  stops again says so again).

A run nobody asked for (Sentry started it) tells the admins instead. Rejected and cancelled
runs tell nobody: a person did that, and knows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import FixKind, FixRun, NotificationKind
from sensei_core.notifications.service import Event, Told, admins, notify, repo_place
from sensei_fixer.approvals import revision

TELLS = frozenset({"awaiting_approval", "done", "not_reproduced", "blocked", "failed"})
STOPPED = {"not_reproduced": "couldn't be reproduced", "blocked": "was blocked",
           "failed": "failed"}  # fmt: skip


async def fix_notices(
    db: AsyncSession, run: FixRun, events: Iterable[dict[str, Any]]
) -> list[Told]:
    """Tell people of ``events`` (just made by ``run``'s state machine), in ``db``'s
    transaction."""
    wanted = [event for event in events if event.get("type") in TELLS]
    if not wanted:
        return []
    place = await repo_place(db, run.org_id, run.repo_id)
    if place is None:
        return []
    change = run.kind is FixKind.CHANGE
    noun = "Change" if change else "Fix"
    link = f"{place.path}/{'changes' if change else 'fixes'}/{run.id}"
    asked = [run.created_by] if run.created_by is not None else []
    told: list[Told] = []
    for event in wanted:
        kind = event["type"]
        if kind == "awaiting_approval":
            rev = revision(run)
            again = f" (revision {rev})" if rev > 1 else ""
            notice = Event(
                run.org_id, NotificationKind.APPROVAL_NEEDED, f"fix:{run.id}:awaiting:{rev}",
                f"{noun} plan to approve{again}: {run.title}",
                f"In {place.full_name}, risk {run.risk.value if run.risk else 'not yet rated'}.",
                link, {"risk": run.risk.value} if run.risk else None, run.repo_id,
            )  # fmt: skip
            users = {*asked, *await admins(db, run.org_id)}
        elif kind == "done":
            if not run.pr_url:
                continue
            notice = Event(
                run.org_id, NotificationKind.PR_OPENED, f"fix:{run.id}:pr",
                f"Draft pull request opened: {run.title}", f"In {place.full_name}: {run.pr_url}",
                link, repo_id=run.repo_id,
            )  # fmt: skip
            users = set(asked or await admins(db, run.org_id))
        else:
            notice = Event(
                run.org_id, NotificationKind.FIX_FAILED, f"fix:{run.id}:{kind}:{event.get('ts')}",
                f"{noun} {STOPPED[kind]}: {run.title}",
                f"In {place.full_name}. {run.stop_reason or ''}".strip(), link,
                repo_id=run.repo_id,
            )  # fmt: skip
            users = set(asked or await admins(db, run.org_id))
        told.append(await notify(db, notice, _sorted(users)))
    return told


def _sorted(users: set[uuid.UUID]) -> list[uuid.UUID]:
    return sorted(users, key=str)
