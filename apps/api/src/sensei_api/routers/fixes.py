"""Fix intake (8.3): starting a fix from a pasted trace or a GitHub issue, listing a
repository's fixes, reading one; and parsing a paste for the intake page's live preview. Then
the run's controls (8.4-8.8): start, cancel, retry with more context, and people's decisions on
its plan (approve, request changes, reject: :mod:`sensei_fixer.approvals`).

A fix needs a member (viewers can read fixes, not start them) who can read the repository on
GitHub; a GitHub issue is read with the user's own GitHub credential, so only issues they can
see come in. The pipeline (8.4) picks queued runs up; nothing runs here.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import EventSourceResponse, ServerSentEvent

from sensei_api import streams
from sensei_api.deps import DbSession, RedisClient, Writable
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoAccessDep, RepoDep, checked, get_viewer, github_client
from sensei_core.audit import AuditAction, audit
from sensei_core.db.tenancy import set_org_context
from sensei_core.errors import ConflictError, ForbiddenError, InvalidError, NotFoundError
from sensei_core.fixes.service import FixService, repo_index
from sensei_core.fixes.tickets import MAX_CONTEXTS, MAX_TEXT, Frame, Ticket, parse_ticket
from sensei_core.github.client import Credential, GitHubError
from sensei_core.jobs import FixJobs
from sensei_core.models import (
    ApprovalDecision,
    FixApproval,
    FixKind,
    FixRisk,
    FixRun,
    FixSource,
    FixStage,
    FixStatus,
    Repo,
    ReproResult,
    Role,
    User,
)
from sensei_core.models.fixes import TERMINAL
from sensei_core.orgs.service import OrgAccess, require_role
from sensei_core.repos.access import RepoAccess
from sensei_fixer.approvals import decide, required
from sensei_fixer.events import FINAL, FixOutbox, fix_stream
from sensei_fixer.pipeline import cancel, close_agent_run
from sensei_fixer.state import FixStateError, FixStateMachine

router = APIRouter(prefix="/orgs/{org}", tags=["fixes"])
MAX_COMMENTS = 50
_ISSUE_URL = re.compile(
    r"^https://github\.com/(?P<full>[\w.-]+/[\w.-]+)/issues/(?P<number>\d+)/?(?:[#?].*)?$"
)


class FrameOut(BaseModel):
    file: str
    line: int
    function: str | None
    path: str | None = Field(description="The repository's file the frame is in, if it is one.")
    in_repo: bool
    library: bool


class TicketOut(BaseModel):
    title: str
    language: Literal["python", "javascript"] | None
    error_type: str | None
    message: str | None
    frames: list[FrameOut]
    versions: dict[str, str]
    culprit: FrameOut | None = Field(description="The innermost frame in the repository.")
    text: str = Field(default="", description="What was given: the paste, the issue, a request.")

    @classmethod
    def build(cls, ticket: Ticket) -> TicketOut:
        def frame(f: Frame) -> FrameOut:
            return FrameOut(**f.model_dump())

        culprit = ticket.culprit
        return cls(
            title=ticket.title, language=ticket.language, error_type=ticket.error_type,
            message=ticket.message, frames=[frame(f) for f in ticket.frames],
            versions=ticket.versions, culprit=frame(culprit) if culprit else None,
            text=ticket.text,
        )  # fmt: skip


class FixSummaryOut(BaseModel):
    id: uuid.UUID
    kind: FixKind = Field(description="A fix (a reported bug) or a change (a request).")
    repo: str
    title: str
    source: FixSource
    source_ref: str | None
    status: FixStatus
    stage: FixStage
    risk: FixRisk | None
    merge_readiness: int | None
    pr_url: str | None
    created_by: uuid.UUID | None
    author: str | None = Field(description="Who asked for it (their login); None: Sentry did.")
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_run(cls, run: FixRun, repo: Repo, author: str | None = None) -> FixSummaryOut:
        return cls(
            id=run.id, kind=run.kind or FixKind.FIX, repo=repo.slug, title=run.title,
            source=run.source,
            source_ref=run.source_ref, status=run.status, stage=run.stage, risk=run.risk,
            merge_readiness=run.merge_readiness, pr_url=run.pr_url, created_by=run.created_by,
            author=author, created_at=run.created_at, finished_at=run.finished_at,
        )  # fmt: skip


class FixOut(FixSummaryOut):
    ticket: TicketOut
    repro_result: ReproResult | None
    root_cause: dict[str, Any] | None
    repro: dict[str, Any] | None = Field(description="The reproducing test and its runs.")
    plan: dict[str, Any] | None
    patch: dict[str, Any] | None = Field(description="The change: its diff, files and attempts.")
    verification: dict[str, Any] | None = Field(
        description="Its verification: runs, the verifier's verdict, risk and merge readiness."
    )
    stop_reason: str | None = Field(description="Why it stopped short, when it did.")
    approvals_needed: int | None = Field(
        description="Distinct approvers its plan needs (two for a high-risk fix); None: no plan."
    )
    run_id: uuid.UUID | None = Field(description="Its agent run (the timeline of its steps).")

    @classmethod
    def full(cls, run: FixRun, repo: Repo, author: str | None = None) -> FixOut:
        summary = FixSummaryOut.from_run(run, repo, author)
        return cls(
            **summary.model_dump(), ticket=TicketOut.build(Ticket.model_validate(run.ticket)),
            repro_result=run.repro_result, root_cause=run.root_cause, repro=run.repro,
            plan=run.plan, patch=run.patch, verification=run.verification,
            stop_reason=run.stop_reason, approvals_needed=required(run.risk) if run.plan else None,
            run_id=run.run_id,
        )  # fmt: skip


async def authors(db: AsyncSession, runs: list[FixRun]) -> dict[uuid.UUID, str]:
    """The logins of who asked for ``runs``, by user id (one query)."""
    ids = {run.created_by for run in runs if run.created_by is not None}
    if not ids:
        return {}
    found = await db.execute(select(User.id, User.login).where(User.id.in_(ids)))
    return dict(found.tuples().all())


async def full(db: AsyncSession, run: FixRun, repo: Repo) -> FixOut:
    """``run`` as the API shows it, with its author's login."""
    known = await authors(db, [run])
    return FixOut.full(run, repo, known.get(run.created_by) if run.created_by else None)


class ParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)


class FixIn(BaseModel):
    source: Literal["paste", "github_issue"]
    text: str | None = Field(default=None, max_length=MAX_TEXT)  # paste: the trace, or words
    title: str | None = Field(default=None, max_length=200)
    issue: str | None = Field(default=None, max_length=300)  # a number, #number or its URL

    @model_validator(mode="after")
    def _given(self) -> FixIn:
        if self.source == "paste" and not (self.text or "").strip():
            raise ValueError("a paste needs its text")
        if self.source == "github_issue" and not (self.issue or "").strip():
            raise ValueError("a GitHub issue needs its number or URL")
        return self


def jobs_of(request: Request) -> FixJobs:
    jobs: FixJobs = request.app.state.fix_jobs
    return jobs


def service_of(request: Request) -> FixService:
    found: FixService | None = getattr(request.app.state, "fix_service", None)
    return found or FixService()


def issue_number(given: str, repo: Repo) -> int:
    """The issue ``given`` names in ``repo``: ``123``, ``#123``, or its URL (this repository's)."""
    text = given.strip()
    if text.lstrip("#").isdigit():
        return int(text.lstrip("#"))
    found = _ISSUE_URL.match(text)
    if found is None:
        raise InvalidError("Give the issue's number or its GitHub URL.", code="invalid_issue")
    if found["full"].lower() != repo.full_name.lower():
        raise InvalidError(f"That issue is in {found['full']}, not {repo.full_name}.",
                           code="issue_elsewhere")  # fmt: skip
    return int(found["number"])


async def _issue_text(
    request: Request, repo: Repo, number: int, viewer: Credential
) -> tuple[str, str, str]:
    """The issue's title, its text (body and comments, where traces get pasted) and URL."""
    client = github_client(request)
    base = f"/repos/{repo.full_name}/issues/{number}"
    try:
        issue = cast("dict[str, Any]", await client.get_json(base, viewer))
        comments = cast(
            "list[dict[str, Any]]",
            await client.get_json(f"{base}/comments", viewer, per_page=MAX_COMMENTS),
        )
    except GitHubError as error:
        if error.status == 404:
            raise NotFoundError(
                "No such issue (or you can't see it).", code="issue_not_found"
            ) from None
        raise
    if issue.get("pull_request"):
        raise InvalidError("That's a pull request, not an issue.", code="not_an_issue")
    parts = [str(issue.get("body") or ""), *(str(c.get("body") or "") for c in comments)]
    return str(issue.get("title") or f"Issue #{number}"), "\n\n".join(parts), str(issue["html_url"])


async def _parse(db: AsyncSession, repo: Repo, text: str, title: str | None = None) -> Ticket:
    """Parsed off the event loop (a long paste takes a moment), against the cached index."""
    index = await repo_index(db, repo)
    return await asyncio.to_thread(parse_ticket, text, title=title, index=index)


@router.post("/repos/{repo}/fixes/parse", response_model=TicketOut)
async def parse_fix(body: ParseIn, access: OrgAccessDep, repo: RepoDep, db: DbSession) -> TicketOut:
    """What a paste parses to (the intake page's preview, for those who can start a fix):
    nothing is saved."""
    require_role(access, Role.MEMBER)
    return TicketOut.build(await _parse(db, repo, body.text))


@router.post(
    "/repos/{repo}/fixes",
    response_model=FixOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Writable],
)
async def create_fix(
    *,
    request: Request,
    response: Response,
    body: FixIn,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    readers: RepoAccessDep,
    background: BackgroundTasks,
) -> FixOut:
    """Start a fix from a pasted trace (or a description) or a GitHub issue. An issue already
    being fixed answers 200 with that fix."""
    require_role(access, Role.MEMBER)  # before asking GitHub anything
    if body.source == "paste":
        ticket = await _parse(db, repo, body.text or "", body.title)
        source, ref = FixSource.PASTE, None
    else:
        number = issue_number(body.issue or "", repo)
        viewer = await get_viewer(access, readers)  # the user's own: their issues only
        title, text, url = await _issue_text(request, repo, number, viewer)
        ticket = await _parse(db, repo, f"{title}\n\n{text}", title)
        source, ref = FixSource.GITHUB_ISSUE, url
    run, new = await service_of(request).create(db, repo, ticket, source=source, source_ref=ref,
                                              access=access)  # fmt: skip
    if not new:
        response.status_code = status.HTTP_200_OK
    elif run.status is FixStatus.QUEUED:
        # After the commit (the worker must find the row): sent once the response is.
        background.add_task(jobs_of(request).run_stage, run.org_id, run.id, run.stage.value)
    return await full(db, run, repo)


@router.get("/repos/{repo}/fixes", response_model=list[FixSummaryOut])
async def list_fixes(
    request: Request,
    repo: RepoDep,
    db: DbSession,
    status_filter: Annotated[FixStatus | None, Query(alias="status")] = None,
) -> list[FixSummaryOut]:
    """The repository's fixes, newest first."""
    runs = await service_of(request).list(db, repo, status=status_filter, kind=FixKind.FIX)
    runs = list(runs)
    known = await authors(db, runs)
    return [
        FixSummaryOut.from_run(run, repo, known.get(run.created_by) if run.created_by else None)
        for run in runs
    ]


def kind_of(request: Request) -> FixKind:
    """The kind of run a route is for: ``/changes/...`` routes (9.1) a change's, the rest a
    fix's. The run's controls (read, events, cancel, decide) are the same endpoints under both."""
    route = request.scope.get("route")
    template = str(getattr(route, "path", ""))  # the route's template, never the URL itself
    return FixKind.CHANGE if "/changes/{fix_id}" in template else FixKind.FIX


def not_found(kind: FixKind) -> NotFoundError:
    return NotFoundError(f"No such {kind.value}.", code=f"{kind.value}_not_found")


async def _visible(
    request: Request, access: OrgAccess, db: AsyncSession, readers: RepoAccess, fix_id: uuid.UUID
) -> tuple[FixRun, Repo]:
    """The run (of the route's kind: a fix's route finds no change) and its repository, if the
    user can read it."""
    kind = kind_of(request)
    try:
        run = await service_of(request).get(db, access.org.id, fix_id)
    except NotFoundError:
        raise not_found(kind) from None
    repo = await db.get(Repo, run.repo_id)
    # As its repository: a run on one the user can't read on GitHub looks like none.
    if (
        (run.kind or FixKind.FIX) is not kind
        or repo is None
        or repo.deleted_at is not None
        or not await checked(readers, access.user, [repo])
    ):
        raise not_found(kind)
    return run, repo


@router.get("/fixes/{fix_id}", response_model=FixOut)
async def get_fix(
    request: Request,
    fix_id: uuid.UUID,
    access: OrgAccessDep,
    db: DbSession,
    readers: RepoAccessDep,
) -> FixOut:
    run, repo = await _visible(request, access, db, readers, fix_id)
    return await full(db, run, repo)


async def _locked(db: AsyncSession, run: FixRun) -> FixRun:
    # populate_existing: the row as it is once locked, not the copy loaded before (a stage
    # may have finished meanwhile).
    found = await db.scalar(
        select(FixRun)
        .where(FixRun.id == run.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if found is None:
        raise NotFoundError("No such fix.", code="fix_not_found")
    return found


@router.post("/fixes/{fix_id}/start", response_model=FixOut, dependencies=[Writable])
async def start_fix(
    *,
    request: Request,
    fix_id: uuid.UUID,
    access: OrgAccessDep,
    db: DbSession,
    readers: RepoAccessDep,
    redis: RedisClient,
    background: BackgroundTasks,
) -> FixOut:
    """Start a fix waiting for triage (a Sentry issue under the gate). Members."""
    require_role(access, Role.MEMBER)
    run, repo = await _visible(request, access, db, readers, fix_id)
    run = await _locked(db, run)
    machine = FixStateMachine(run)
    try:
        machine.start()
    except FixStateError as error:
        raise ConflictError(str(error), code="fix_not_waiting") from None
    outbox = FixOutbox(run.org_id, run.id, run.repo_id)
    outbox.add(machine.events)
    background.add_task(outbox.publish, redis)
    background.add_task(jobs_of(request).run_stage, run.org_id, run.id, run.stage.value)
    return await full(db, run, repo)


@router.post("/fixes/{fix_id}/cancel", response_model=FixOut)
async def cancel_fix(
    *,
    request: Request,
    fix_id: uuid.UUID,
    access: OrgAccessDep,
    db: DbSession,
    readers: RepoAccessDep,
    redis: RedisClient,
    background: BackgroundTasks,
) -> FixOut:
    """Cancel a fix that hasn't finished. Members. A stage still working stops at its next
    check, and what it found is dropped."""
    require_role(access, Role.MEMBER)
    run, repo = await _visible(request, access, db, readers, fix_id)
    run = await _locked(db, run)
    try:
        events = cancel(run, by=f"@{access.user.login}")
    except FixStateError as error:
        raise ConflictError(str(error), code="fix_finished") from None
    await close_agent_run(db, run)
    outbox = FixOutbox(run.org_id, run.id, run.repo_id)
    outbox.add(events)
    background.add_task(outbox.publish, redis)
    return await full(db, run, repo)


class RetryIn(BaseModel):
    context: str = Field(min_length=1, max_length=4_000)


@router.post("/fixes/{fix_id}/retry", response_model=FixOut, dependencies=[Writable])
async def retry_fix(
    *,
    request: Request,
    fix_id: uuid.UUID,
    body: RetryIn,
    access: OrgAccessDep,
    db: DbSession,
    readers: RepoAccessDep,
    redis: RedisClient,
    background: BackgroundTasks,
) -> FixOut:
    """Try a fix that couldn't be reproduced again, with more context (how to trigger it, the
    data involved): from the investigation. Members."""
    require_role(access, Role.MEMBER)
    run, repo = await _visible(request, access, db, readers, fix_id)
    run = await _locked(db, run)
    machine = FixStateMachine(run)
    try:
        machine.retry()
    except FixStateError as error:
        raise ConflictError(str(error), code="fix_not_retryable") from None
    ticket = Ticket.model_validate(run.ticket)
    added = f"From @{access.user.login}: {body.context.strip()}"
    run.ticket = ticket.model_copy(update={"context": [*ticket.context, added][-MAX_CONTEXTS:]}
                                   ).model_dump(mode="json")  # fmt: skip
    outbox = FixOutbox(run.org_id, run.id, run.repo_id)
    outbox.add(machine.events)
    background.add_task(outbox.publish, redis)
    background.add_task(jobs_of(request).run_stage, run.org_id, run.id, run.stage.value)
    return await full(db, run, repo)


class StepEdit(BaseModel):
    description: str = Field(min_length=1, max_length=1_000)
    path: str = Field(min_length=1, max_length=500)


class FileEdit(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    change: Literal["modify", "create"]
    why: str = Field(default="", max_length=500)


class TestEdit(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    checks: str = Field(default="", max_length=500)


class PlanEdits(BaseModel):
    """The plan as a person edited it (each part left out stays as the planner had it; a
    change's plan has its new tests too)."""

    summary: str | None = Field(default=None, max_length=2_000)
    steps: list[StepEdit] | None = Field(default=None, max_length=10)
    files: list[FileEdit] | None = Field(default=None, max_length=12)
    tests: list[TestEdit] | None = Field(default=None, max_length=5)


class ApprovalIn(BaseModel):
    decision: ApprovalDecision
    plan_revision: int = Field(ge=0, description="The plan's revision decided on (as read).")
    comment: str | None = Field(default=None, max_length=4_000)
    plan_edits: PlanEdits | None = Field(
        default=None, description="An edited plan (with request_changes): planned again."
    )


class ApprovalOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    login: str | None
    decision: ApprovalDecision
    comment: str | None
    plan_edits: dict[str, Any] | None
    plan_revision: int
    created_at: datetime


@router.post("/fixes/{fix_id}/approval", response_model=FixOut, dependencies=[Writable])
async def decide_fix(
    *,
    request: Request,
    fix_id: uuid.UUID,
    body: ApprovalIn,
    access: OrgAccessDep,
    db: DbSession,
    readers: RepoAccessDep,
    redis: RedisClient,
    background: BackgroundTasks,
) -> FixOut:
    """Approve the fix's plan, ask for changes (planned again), or reject it. Members who can
    write to the repository on GitHub (asked now); a high-risk fix needs two of them, and whoever
    asked for it can't be the only one. Every decision is audited."""
    require_role(access, Role.MEMBER)
    run, repo = await _visible(request, access, db, readers, fix_id)
    if not await readers.can_write(access.user, repo):
        raise ForbiddenError(
            "Deciding on its plan needs write access to the repository on GitHub.",
            code="github_write_required",
        )
    run = await _locked(db, run)
    edits = body.plan_edits.model_dump(exclude_none=True) if body.plan_edits else None
    done = await decide(
        db, run, user_id=access.user.id, login=access.user.login, decision=body.decision,
        plan_revision=body.plan_revision, comment=body.comment, plan_edits=edits or None,
    )  # fmt: skip
    await audit(
        db, AuditAction.FIX_DECISION, actor_user_id=access.user.id, org_id=access.org.id,
        target_type="fix", target_id=run.id,
        meta={"kind": (run.kind or FixKind.FIX).value, "decision": body.decision.value,
              "plan_revision": body.plan_revision,
              "risk": run.risk.value if run.risk else None, "approvals": done.approved_by,
              "needed": done.needed, "status": run.status.value},
    )  # fmt: skip
    await close_agent_run(db, run)  # a rejection ends the run
    outbox = FixOutbox(run.org_id, run.id, run.repo_id)
    outbox.add(done.events)
    background.add_task(outbox.publish, redis)
    if done.queue:
        background.add_task(jobs_of(request).run_stage, run.org_id, run.id, run.stage.value)
    return await full(db, run, repo)


@router.get("/fixes/{fix_id}/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    request: Request,
    fix_id: uuid.UUID,
    access: OrgAccessDep,
    db: DbSession,
    readers: RepoAccessDep,
) -> list[ApprovalOut]:
    """The decisions on the fix's plans, oldest first."""
    run, _repo = await _visible(request, access, db, readers, fix_id)
    rows = await db.execute(
        select(FixApproval, User.login)
        .outerjoin(User, User.id == FixApproval.user_id)
        .where(FixApproval.fix_run_id == run.id)
        .order_by(FixApproval.created_at, FixApproval.id)
    )
    return [
        ApprovalOut(
            id=row.id,
            user_id=row.user_id,
            login=login,
            decision=row.decision,
            comment=row.comment,
            plan_edits=row.plan_edits,
            plan_revision=row.plan_revision,
            created_at=row.created_at,
        )
        for row, login in rows.tuples()
    ]


def _final(run: FixRun) -> dict[str, Any]:
    kind = "done" if run.status is FixStatus.SUCCEEDED else run.status.value
    return {
        "type": kind,
        "fix_id": str(run.id),
        "stage": run.stage.value,
        "status": run.status.value,
        "reason": run.stop_reason,
        "pr_url": run.pr_url,
    }


@router.get(
    "/fixes/{fix_id}/events",
    response_class=EventSourceResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def fix_events(
    *,
    request: Request,
    fix_id: uuid.UUID,
    access: OrgAccessDep,
    db: DbSession,
    readers: RepoAccessDep,
    redis: RedisClient,
) -> EventSourceResponse:
    """The fix's progress as server-sent events (types: queued, stage_started, step, log,
    stage_done, awaiting_approval, approved, replanning, done, not_reproduced, blocked, failed,
    rejected, cancelled). Replays from ``Last-Event-ID``; closes after the final event."""
    run, _ = await _visible(request, access, db, readers, fix_id)
    cursor = streams.last_event_id(request)
    org_id = run.org_id
    latest: list[FixRun] = [run]

    async def going() -> bool:
        async with streams.short_session(request) as session:
            await set_org_context(session, org_id)
            current = await session.get(FixRun, fix_id)
            if current is None:
                return False
            latest[0] = current
            return current.status not in TERMINAL

    async def events() -> AsyncGenerator[ServerSentEvent]:
        ended = False
        async for item in streams.follow(
            request, redis, fix_stream(fix_id), cursor, stop_on=FINAL,
            follow_new=run.status not in TERMINAL, still=going,
        ):  # fmt: skip
            ended = ended or item.event in FINAL
            yield item
        if ended or await request.is_disconnected():
            return
        if latest[0].status not in TERMINAL:
            await going()  # the last word from the database
        if latest[0].status in TERMINAL:
            # Over, but the stream doesn't say so (it expired, Redis was down): the row does.
            yield streams.event(_final(latest[0]))

    return streams.response(events())
