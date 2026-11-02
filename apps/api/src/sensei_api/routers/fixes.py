"""Fix intake (8.3): starting a fix from a pasted trace or a GitHub issue, listing a
repository's fixes, reading one; and parsing a paste for the intake page's live preview.

A fix needs a member (viewers can read fixes, not start them) who can read the repository on
GitHub; a GitHub issue is read with the user's own GitHub credential, so only issues they can
see come in. The pipeline (8.4) picks queued runs up; nothing runs here.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from sensei_api.deps import DbSession
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoAccessDep, RepoDep, checked, get_viewer, github_client
from sensei_core.errors import InvalidError, NotFoundError
from sensei_core.fixes.service import FixService, repo_paths
from sensei_core.fixes.tickets import MAX_TEXT, Frame, Ticket, parse_ticket
from sensei_core.github.client import Credential, GitHubError
from sensei_core.models import (
    FixRisk,
    FixRun,
    FixSource,
    FixStage,
    FixStatus,
    Repo,
    ReproResult,
    Role,
)
from sensei_core.orgs.service import require_role

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

    @classmethod
    def build(cls, ticket: Ticket) -> TicketOut:
        def frame(f: Frame) -> FrameOut:
            return FrameOut(**f.model_dump())

        culprit = ticket.culprit
        return cls(
            title=ticket.title, language=ticket.language, error_type=ticket.error_type,
            message=ticket.message, frames=[frame(f) for f in ticket.frames],
            versions=ticket.versions, culprit=frame(culprit) if culprit else None,
        )  # fmt: skip


class FixSummaryOut(BaseModel):
    id: uuid.UUID
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
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_run(cls, run: FixRun, repo: Repo) -> FixSummaryOut:
        return cls(
            id=run.id, repo=repo.slug, title=run.title, source=run.source,
            source_ref=run.source_ref, status=run.status, stage=run.stage, risk=run.risk,
            merge_readiness=run.merge_readiness, pr_url=run.pr_url, created_by=run.created_by,
            created_at=run.created_at, finished_at=run.finished_at,
        )  # fmt: skip


class FixOut(FixSummaryOut):
    ticket: TicketOut
    repro_result: ReproResult | None
    root_cause: dict[str, Any] | None
    plan: dict[str, Any] | None

    @classmethod
    def full(cls, run: FixRun, repo: Repo) -> FixOut:
        summary = FixSummaryOut.from_run(run, repo)
        return cls(
            **summary.model_dump(), ticket=TicketOut.build(Ticket.model_validate(run.ticket)),
            repro_result=run.repro_result, root_cause=run.root_cause, plan=run.plan,
        )  # fmt: skip


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


def _service(request: Request) -> FixService:
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


@router.post("/repos/{repo}/fixes/parse", response_model=TicketOut)
async def parse_fix(body: ParseIn, repo: RepoDep, db: DbSession) -> TicketOut:
    """What a paste parses to (the intake page's preview): nothing is saved."""
    return TicketOut.build(parse_ticket(body.text, paths=await repo_paths(db, repo)))


@router.post("/repos/{repo}/fixes", response_model=FixOut, status_code=status.HTTP_201_CREATED)
async def create_fix(
    *,
    request: Request,
    response: Response,
    body: FixIn,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    readers: RepoAccessDep,
) -> FixOut:
    """Start a fix from a pasted trace (or a description) or a GitHub issue. An issue already
    being fixed answers 200 with that fix."""
    require_role(access, Role.MEMBER)  # before asking GitHub anything
    paths = await repo_paths(db, repo)
    if body.source == "paste":
        ticket = parse_ticket(body.text or "", title=body.title, paths=paths)
        source, ref = FixSource.PASTE, None
    else:
        number = issue_number(body.issue or "", repo)
        viewer = await get_viewer(access, readers)  # the user's own: their issues only
        title, text, url = await _issue_text(request, repo, number, viewer)
        ticket = parse_ticket(f"{title}\n\n{text}", title=title, paths=paths)
        source, ref = FixSource.GITHUB_ISSUE, url
    run, new = await _service(request).create(db, repo, ticket, source=source, source_ref=ref,
                                              access=access)  # fmt: skip
    if not new:
        response.status_code = status.HTTP_200_OK
    return FixOut.full(run, repo)


@router.get("/repos/{repo}/fixes", response_model=list[FixSummaryOut])
async def list_fixes(
    request: Request,
    repo: RepoDep,
    db: DbSession,
    status_filter: Annotated[FixStatus | None, Query(alias="status")] = None,
) -> list[FixSummaryOut]:
    """The repository's fixes, newest first."""
    runs = await _service(request).list(db, repo, status=status_filter)
    return [FixSummaryOut.from_run(run, repo) for run in runs]


@router.get("/fixes/{fix_id}", response_model=FixOut)
async def get_fix(
    request: Request,
    fix_id: uuid.UUID,
    access: OrgAccessDep,
    db: DbSession,
    readers: RepoAccessDep,
) -> FixOut:
    run = await _service(request).get(db, access.org.id, fix_id)
    repo = await db.get(Repo, run.repo_id)
    # As its repository: a fix on one the user can't read on GitHub looks like no fix.
    if (
        repo is None
        or repo.deleted_at is not None
        or not await checked(readers, access.user, [repo])
    ):
        raise NotFoundError("No such fix.", code="fix_not_found")
    return FixOut.full(run, repo)
