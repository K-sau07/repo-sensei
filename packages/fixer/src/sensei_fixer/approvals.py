"""People's decisions on a fix's plan (8.8): approve it, ask for changes, or reject it.

Who may decide is the caller's to check (an org member with write access to the repository on
GitHub, asked live); this module holds the rules of the decision itself:

- a decision is on one **revision** of the plan (the one the person read): a plan planned again
  meanwhile is a different plan, and the decision is refused;
- **approve**: the run goes on to the patch once enough distinct people approved this revision:
  one, or two for a **high-risk** fix. Whoever asked for the fix may be one of the two, never the
  only one; approving twice counts once (and is refused);
- **request changes** (a comment, an edited plan, or both): the plan is planned again with what
  they asked, then waits for approval again; approvals of the old plan don't count for the new;
- **reject**: the run stops, rejected, saying who and why.

Each decision is a row of ``fix_approvals`` (with the revision), and the caller audits it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.errors import ConflictError, InvalidError
from sensei_core.models import ApprovalDecision, FixApproval, FixRisk, FixRun, FixStatus
from sensei_fixer.state import FixStateMachine

MAX_REQUESTS = 5  # requests for changes kept on a plan (the planner reads the last few)


def utcnow() -> datetime:
    return datetime.now(UTC)


def required(risk: FixRisk | None) -> int:
    """Distinct approvers a plan needs: two for a high-risk fix (or one whose risk is unknown),
    one otherwise."""
    return 1 if risk in (FixRisk.LOW, FixRisk.MEDIUM) else 2


def revision(run: FixRun) -> int:
    """The plan's revision (1 for the first plan; 0: none yet)."""
    plan = run.plan or {}
    found = plan.get("revision")
    return found if isinstance(found, int) and not isinstance(found, bool) else 0


async def approvers(db: AsyncSession, run: FixRun, plan_revision: int) -> set[uuid.UUID]:
    """Who has approved ``plan_revision`` of ``run``'s plan."""
    found = await db.scalars(
        select(FixApproval.user_id).where(
            FixApproval.fix_run_id == run.id,
            FixApproval.plan_revision == plan_revision,
            FixApproval.decision == ApprovalDecision.APPROVE,
        )
    )
    return set(found)


@dataclass
class Decided:
    """What a decision did: its row, the run's events (to publish once committed), whether the
    next stage is queued, and the approvals so far against those needed."""

    approval: FixApproval
    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    queue: bool = False
    approved_by: int = 0
    needed: int = 1


async def decide(
    db: AsyncSession,
    run: FixRun,
    *,
    user_id: uuid.UUID,
    login: str,
    decision: ApprovalDecision,
    plan_revision: int,
    comment: str | None = None,
    plan_edits: Mapping[str, Any] | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> Decided:
    """Record ``user_id``'s decision on ``run`` (locked by the caller) and apply it."""
    if run.status is not FixStatus.AWAITING_APPROVAL:
        raise ConflictError(f"The fix isn't waiting for approval (it is {run.status.value}).",
                            code="fix_not_awaiting_approval")  # fmt: skip
    current = revision(run)
    if plan_revision != current:
        raise ConflictError(
            f"The plan changed (revision {current}, not {plan_revision}): read it again.",
            code="fix_plan_changed",
        )
    said = (comment or "").strip() or None
    if decision is ApprovalDecision.REQUEST_CHANGES and not (said or plan_edits):
        raise InvalidError("Say what to change: a comment, an edited plan, or both.",
                           code="fix_changes_unsaid")  # fmt: skip
    if plan_edits and decision is not ApprovalDecision.REQUEST_CHANGES:
        raise InvalidError("An edited plan is planned again: send it as request_changes.",
                           code="fix_edits_need_replan")  # fmt: skip
    needed = required(run.risk)
    approved = await approvers(db, run, current)
    if decision is ApprovalDecision.APPROVE and user_id in approved:
        raise ConflictError("You have approved this plan already.", code="fix_already_approved")
    row = FixApproval(
        org_id=run.org_id, fix_run_id=run.id, user_id=user_id, decision=decision, comment=said,
        plan_edits=dict(plan_edits) if plan_edits else None, plan_revision=current,
    )  # fmt: skip
    db.add(row)
    await db.flush()
    machine = FixStateMachine(run, clock)
    done = Decided(row, needed=needed)
    if decision is ApprovalDecision.APPROVE:
        approved.add(user_id)
        done.approved_by = len(approved)
        if len(approved) >= needed:
            machine.approve()
            done.queue = True
        else:
            done.events.append(_event(run, clock, "approval", by=login, approvals=len(approved),
                                      needed=needed))  # fmt: skip
    elif decision is ApprovalDecision.REQUEST_CHANGES:
        request = {"by": login, "comment": said, "edits": dict(plan_edits) if plan_edits else None,
                   "revision": current}  # fmt: skip
        plan = dict(run.plan or {})
        requests = [*list(plan.get("requests") or []), request][-MAX_REQUESTS:]
        run.plan = {**plan, "requests": requests}
        machine.replan()
        done.queue = True
    else:
        why = f": {said}" if said else "."
        machine.stop(FixStatus.REJECTED, f"Rejected by @{login}{why}")
    done.events = [*done.events, *machine.events]
    return done


def _event(run: FixRun, clock: Callable[[], datetime], kind: str, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "fix_id": str(run.id), "stage": run.stage.value,
            "status": run.status.value, "ts": clock().isoformat(), **extra}  # fmt: skip
