"""Requested changes (9.1): a change described in words, planned, approved, built and verified
by the fix pipeline (:mod:`sensei_fixer`), to a draft pull request.

A change is a run of its own kind (``kind = change``): its stages are the fix's without the
investigation and reproduction (intake, plan, approval, patch, verify, pull request). Starting
one, listing them and reading one are here; its controls (its events, cancel, decisions on its
plan) are the fix's own endpoints, served under ``/changes/...`` too (they tell the two apart by
the route: :func:`sensei_api.routers.fixes.kind_of`). Members start changes; viewers read them.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, Request, status
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse

from sensei_api.deps import DbSession, RedisClient, Writable
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoAccessDep, RepoDep
from sensei_api.routers.fixes import (
    ApprovalIn,
    ApprovalOut,
    FixOut,
    FixSummaryOut,
    PlanEdits,
    authors,
    cancel_fix,
    decide_fix,
    fix_events,
    full,
    get_fix,
    jobs_of,
    list_approvals,
    service_of,
)
from sensei_core.fixes.tickets import Ticket
from sensei_core.models import ApprovalDecision, FixKind, FixSource, FixStatus, Role
from sensei_core.orgs.service import require_role

router = APIRouter(prefix="/orgs/{org}", tags=["changes"])
MAX_REQUEST = 4_000
TITLE_CHARS = 120


class ChangeIn(BaseModel):
    request: str = Field(
        min_length=1, max_length=MAX_REQUEST, description="The change: what it should do, and why."
    )
    title: str | None = Field(default=None, max_length=200)


def ticket_of(body: ChangeIn) -> Ticket:
    """A request as the pipeline reads it: its words, and a title (its first line, by default)."""
    text = body.request.strip()
    first = text.splitlines()[0] if text else ""
    title = (body.title or "").strip() or (
        first if len(first) <= TITLE_CHARS else f"{first[: TITLE_CHARS - 1].rstrip()}…"
    )
    return Ticket(title=title, text=text)


@router.post(
    "/repos/{repo}/changes",
    response_model=FixOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Writable],
)
async def create_change(
    *,
    request: Request,
    body: ChangeIn,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    background: BackgroundTasks,
) -> FixOut:
    """Start a change from a request: it is planned, then waits for approval. Members."""
    require_role(access, Role.MEMBER)
    run, _new = await service_of(request).create(
        db, repo, ticket_of(body), source=FixSource.REQUEST, access=access, kind=FixKind.CHANGE
    )
    # After the commit (the worker must find the row): sent once the response is.
    background.add_task(jobs_of(request).run_stage, run.org_id, run.id, run.stage.value)
    return await full(db, run, repo)


@router.get("/repos/{repo}/changes", response_model=list[FixSummaryOut])
async def list_changes(
    request: Request,
    repo: RepoDep,
    db: DbSession,
    status_filter: Annotated[FixStatus | None, Query(alias="status")] = None,
) -> list[FixSummaryOut]:
    """The repository's requested changes, newest first."""
    runs = list(await service_of(request).list(db, repo, status=status_filter, kind=FixKind.CHANGE))
    known = await authors(db, runs)
    return [
        FixSummaryOut.from_run(run, repo, known.get(run.created_by) if run.created_by else None)
        for run in runs
    ]


class PlanEditIn(BaseModel):
    plan_revision: int = Field(ge=0, description="The plan's revision edited (as read).")
    edits: PlanEdits
    comment: str | None = Field(default=None, max_length=4_000)


@router.patch("/changes/{fix_id}/plan", response_model=FixOut, dependencies=[Writable])
async def edit_change_plan(
    *,
    request: Request,
    fix_id: uuid.UUID,
    body: PlanEditIn,
    access: OrgAccessDep,
    db: DbSession,
    readers: RepoAccessDep,
    redis: RedisClient,
    background: BackgroundTasks,
) -> FixOut:
    """Edit the change's plan (its summary, files, steps): it is planned again with the edits,
    then waits for approval again. The same rules as any decision on it: members who can write
    to the repository on GitHub; the revision read must be the current one; audited."""
    decision = ApprovalIn(
        decision=ApprovalDecision.REQUEST_CHANGES, plan_revision=body.plan_revision,
        comment=body.comment, plan_edits=body.edits,
    )  # fmt: skip
    return await decide_fix(
        request=request, fix_id=fix_id, body=decision, access=access, db=db, readers=readers,
        redis=redis, background=background,
    )  # fmt: skip


# The run's controls, the fix's endpoints under /changes (a change's route finds no fix).
_DESCRIPTIONS = {
    "get": "A change: its request, plan, patch and verification as they stand.",
    "events": "The change's progress as server-sent events (as a fix's: queued, stage_started, "
    "step, awaiting_approval, approved, replanning, done, failed...).",
    "cancel": "Cancel a change that hasn't finished. Members.",
    "approval": "Approve the change's plan, ask for changes (planned again), or reject it. "
    "Members who can write to the repository on GitHub; a high-risk change needs two of them.",
    "approvals": "The decisions on the change's plans, oldest first.",
}
router.add_api_route("/changes/{fix_id}", get_fix, methods=["GET"], response_model=FixOut,
                     name="get_change", description=_DESCRIPTIONS["get"])  # fmt: skip
router.add_api_route(
    "/changes/{fix_id}/events", fix_events, methods=["GET"], response_class=EventSourceResponse,
    responses={200: {"content": {"text/event-stream": {}}}}, name="change_events",
    description=_DESCRIPTIONS["events"],
)  # fmt: skip
router.add_api_route("/changes/{fix_id}/cancel", cancel_fix, methods=["POST"],
                     response_model=FixOut, name="cancel_change",
                     description=_DESCRIPTIONS["cancel"])  # fmt: skip
router.add_api_route("/changes/{fix_id}/approval", decide_fix, methods=["POST"],
                     response_model=FixOut, name="decide_change",
                     description=_DESCRIPTIONS["approval"])  # fmt: skip
router.add_api_route("/changes/{fix_id}/approvals", list_approvals, methods=["GET"],
                     response_model=list[ApprovalOut], name="list_change_approvals",
                     description=_DESCRIPTIONS["approvals"])  # fmt: skip
