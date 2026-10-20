"""Connecting repositories to an org, importing public ones, their settings, and disconnecting.

Connected repositories start ``pending``; indexing picks them up from there. Admins and owners
connect, import, change settings and disconnect; any member can list what they can read
(see :mod:`sensei_core.repos.access`).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit
from sensei_core.errors import ConflictError, DomainError, InvalidError, NotFoundError
from sensei_core.github.app import GitHubApp
from sensei_core.github.client import Credential, GitHubClient, GitHubError, RateLimited, as_object
from sensei_core.models import GitHubInstallation, Repo, RepoSource, RepoStatus, Role
from sensei_core.orgs.service import OrgAccess, require_role
from sensei_core.repos.estimate import IndexEstimate, estimate_index
from sensei_core.repos.urls import parse_github_url
from sensei_core.usage import CostCalculator

MAX_LISTED = 5_000  # repositories scanned across an org's installations for the picker
# Org-level pages share the URL space with repositories (/acme/settings vs /acme/<repo>).
RESERVED_REPO_SLUGS = frozenset(
    {"activity", "ask", "estimate", "import", "inbox", "insights", "new", "repos", "search",
     "settings"}
)  # fmt: skip
_SLUG_CLEAN = re.compile(r"[^a-z0-9._-]+")


class GitHubUnavailableError(DomainError):
    status_code = 503
    code = "github_unavailable"


class GitHubReauthRequiredError(DomainError):
    """The user's GitHub token no longer works: they must sign in with GitHub again."""

    status_code = 401
    code = "github_reauth_required"


def _reauth() -> GitHubReauthRequiredError:
    return GitHubReauthRequiredError("Sign in with GitHub again to continue.")


def _now() -> datetime:
    return datetime.now(UTC)


def _valid_branch(name: str) -> bool:
    """git's rules for a branch name (``git check-ref-format --branch``), and never anything
    that could read as a command-line option."""
    return not (
        not name
        or len(name) > 255
        or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", name)
        or name.startswith(("-", "/"))
        or name.endswith(("/", ".", ".lock"))
        or ".." in name
        or "//" in name
        or "@{" in name
        or name == "@"
        or any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))
    )


class RepoSettings(BaseModel):
    """What admins can change about a connected repository."""

    tracked_branches: list[str] = Field(default_factory=list, max_length=20)
    ignore_globs: list[str] = Field(default_factory=list, max_length=200)
    auto_index: bool = True

    @field_validator("tracked_branches")
    @classmethod
    def _branches(cls, value: list[str]) -> list[str]:
        cleaned = [branch.strip() for branch in value]
        for branch in cleaned:
            if not _valid_branch(branch):
                msg = f"not a valid branch name: {branch!r}"
                raise ValueError(msg)
        return list(dict.fromkeys(cleaned))

    @field_validator("ignore_globs")
    @classmethod
    def _globs(cls, value: list[str]) -> list[str]:
        cleaned = [glob.strip() for glob in value if glob.strip()]
        if any(len(glob) > 200 or "\x00" in glob for glob in cleaned):
            msg = "ignore patterns must be at most 200 characters"
            raise ValueError(msg)
        return list(dict.fromkeys(cleaned))


@dataclass(frozen=True, slots=True)
class AvailableRepo:
    """A repository an org's installations can read, for the "connect a repository" picker."""

    github_repo_id: int
    full_name: str
    private: bool
    default_branch: str
    description: str | None
    language: str | None
    pushed_at: str | None
    account_login: str
    connected: bool


@dataclass(frozen=True, slots=True)
class RepoFacts:
    """What GitHub says about a repository, before it is connected."""

    body: dict[str, Any]
    languages: dict[str, int]
    installation: GitHubInstallation | None

    @property
    def size_kb(self) -> int:
        return int(self.body.get("size") or 0)


def _repo_object(body: object) -> dict[str, Any]:
    repo = as_object(body)
    if repo is None or not isinstance(repo.get("id"), int) or not repo.get("full_name"):
        raise GitHubUnavailableError("GitHub sent an unexpected response.")
    return repo


def _languages(body: object) -> dict[str, int]:
    raw = as_object(body) or {}
    return {str(k): int(v) for k, v in raw.items() if isinstance(v, int)}


class RepoService:
    def __init__(self, app: GitHubApp | None, client: GitHubClient, calc: CostCalculator) -> None:
        self._app = app
        self._client = client  # anonymous calls (public repositories)
        self._calc = calc

    # ------------------------------------------------------------------ GitHub side

    async def _installations(self, db: AsyncSession, access: OrgAccess) -> list[GitHubInstallation]:
        rows = await db.scalars(
            select(GitHubInstallation)
            .where(
                GitHubInstallation.org_id == access.org.id,
                GitHubInstallation.deleted_at.is_(None),
                GitHubInstallation.suspended_at.is_(None),
            )
            .order_by(GitHubInstallation.account_login)
        )
        return list(rows)

    async def available(
        self, db: AsyncSession, access: OrgAccess, viewer: Credential, *, query: str = ""
    ) -> list[AvailableRepo]:
        """The repositories the org's installations cover that the admin (``viewer``, their own
        GitHub credential) can read too, filtered by ``query``."""
        require_role(access, Role.ADMIN)
        if self._app is None:
            return []
        connected = set(
            await db.scalars(
                select(Repo.github_repo_id).where(
                    Repo.org_id == access.org.id, Repo.deleted_at.is_(None)
                )
            )
        )
        needle = query.strip().lower()
        found: dict[int, AvailableRepo] = {}
        for installation in await self._installations(db, access):
            path = f"/user/installations/{installation.installation_id}/repositories"
            try:
                async for item in self._app.client.paginate(path, viewer, item_key="repositories"):
                    repo = as_object(item)
                    if repo is None or not isinstance(repo.get("id"), int):
                        continue
                    full_name = str(repo.get("full_name", ""))
                    if needle and needle not in full_name.lower():
                        continue
                    found.setdefault(
                        repo["id"],
                        AvailableRepo(
                            github_repo_id=repo["id"],
                            full_name=full_name,
                            private=bool(repo.get("private")),
                            default_branch=str(repo.get("default_branch") or "main"),
                            description=repo.get("description"),
                            language=repo.get("language"),
                            pushed_at=repo.get("pushed_at"),
                            account_login=installation.account_login,
                            connected=repo["id"] in connected,
                        ),
                    )
                    if len(found) >= MAX_LISTED:
                        break
            except GitHubError as exc:
                if exc.status in (403, 404):  # the admin can't see this installation on GitHub
                    continue
                if exc.status == 401:
                    raise _reauth() from exc
                raise GitHubUnavailableError("Couldn't list repositories on GitHub.") from exc
        return sorted(found.values(), key=lambda repo: repo.full_name.lower())

    async def _facts_via_installation(
        self, db: AsyncSession, access: OrgAccess, github_repo_id: int, viewer: Credential
    ) -> RepoFacts:
        """Find which of the org's installations covers ``github_repo_id``, which the admin
        (``viewer``) must be able to read on GitHub themselves."""
        not_found = NotFoundError(
            "None of this organization's GitHub installations can read that repository.",
            code="repo_not_in_installation",
        )
        if self._app is not None and not await self._viewer_reads(viewer, github_repo_id):
            raise not_found
        if self._app is not None:
            for installation in await self._installations(db, access):
                credential = await self._app.installation_credential(installation.installation_id)
                try:
                    body = _repo_object(
                        await self._app.client.get_json(
                            f"/repositories/{github_repo_id}", credential
                        )
                    )
                    languages = _languages(
                        await self._app.client.get_json(
                            f"/repos/{body['full_name']}/languages", credential
                        )
                    )
                except GitHubError as exc:
                    if exc.status in (403, 404):
                        continue
                    raise GitHubUnavailableError("GitHub is not responding.") from exc
                return RepoFacts(body=body, languages=languages, installation=installation)
        raise not_found

    async def _viewer_reads(self, viewer: Credential, github_repo_id: int) -> bool:
        try:
            await self._client.get_json(f"/repositories/{github_repo_id}", viewer)
        except GitHubError as exc:
            if exc.status in (403, 404):
                return False
            if exc.status == 401:
                raise _reauth() from exc
            raise GitHubUnavailableError("GitHub is not responding.") from exc
        return True

    async def _facts_public(self, url: str, viewer: Credential | None) -> RepoFacts:
        """Facts about a public repository, asked with the admin's own credential when there is
        one (their own rate limit, not the server's shared anonymous one)."""
        owner, name = parse_github_url(url, web_url=self._client.web_url)
        path = f"/repos/{quote(owner)}/{quote(name)}"
        anonymous = viewer or Credential.anonymous()
        not_public = InvalidError(
            f"{owner}/{name} isn't a public GitHub repository (or doesn't exist). Private "
            "repositories connect through the GitHub App.",
            code="repo_not_public",
        )
        try:
            body = _repo_object(await self._client.get_json(path, anonymous))
            # Asked with the admin's token, a private repository they can read answers too.
            if body.get("private") or body.get("visibility") not in (None, "public"):
                raise not_public
            languages = _languages(await self._client.get_json(f"{path}/languages", anonymous))
        except RateLimited as exc:
            raise GitHubUnavailableError(
                "GitHub's request limit is reached. Try again later, or connect the repository "
                "through the GitHub App.",
                code="github_rate_limited",
            ) from exc
        except GitHubError as exc:
            if exc.status in (403, 404):
                raise not_public from None
            raise GitHubUnavailableError("GitHub is not responding.") from exc
        return RepoFacts(body=body, languages=languages, installation=None)

    # ------------------------------------------------------------------ connect / import

    async def connect(
        self, db: AsyncSession, access: OrgAccess, github_repo_id: int, viewer: Credential
    ) -> Repo:
        require_role(access, Role.ADMIN)
        facts = await self._facts_via_installation(db, access, github_repo_id, viewer)
        return await self._save(db, access, facts, RepoSource.GITHUB_APP)

    async def import_public(
        self, db: AsyncSession, access: OrgAccess, url: str, viewer: Credential | None
    ) -> Repo:
        require_role(access, Role.ADMIN)
        facts = await self._facts_public(url, viewer)
        return await self._save(db, access, facts, RepoSource.PUBLIC_URL)

    async def estimate(
        self,
        db: AsyncSession,
        access: OrgAccess,
        *,
        viewer: Credential,
        url: str | None = None,
        github_repo_id: int | None = None,
    ) -> tuple[RepoFacts, IndexEstimate]:
        require_role(access, Role.ADMIN)
        if (url is None) == (github_repo_id is None):
            raise InvalidError("Give either a URL or a GitHub repository id.", code="invalid_input")
        facts = (
            await self._facts_public(url, viewer)
            if url is not None
            else await self._facts_via_installation(db, access, github_repo_id or 0, viewer)
        )
        return facts, estimate_index(facts.size_kb, facts.languages, self._calc)

    async def _slug_for(self, db: AsyncSession, access: OrgAccess, owner: str, name: str) -> str:
        taken = set(
            await db.scalars(
                select(Repo.slug).where(Repo.org_id == access.org.id, Repo.deleted_at.is_(None))
            )
        )
        base = _SLUG_CLEAN.sub("-", name.lower()).strip("-.") or "repo"
        candidates = [base, _SLUG_CLEAN.sub("-", f"{owner}-{name}".lower()).strip("-.")]
        candidates += [f"{candidates[1]}-{n}" for n in range(2, 100)]
        for candidate in candidates:
            if candidate not in taken and candidate not in RESERVED_REPO_SLUGS:
                return candidate[:201]
        raise ConflictError("Too many repositories with that name.", code="repo_slug_taken")

    async def _save(
        self, db: AsyncSession, access: OrgAccess, facts: RepoFacts, source: RepoSource
    ) -> Repo:
        body = facts.body
        github_repo_id = int(body["id"])
        owner = str((as_object(body.get("owner")) or {}).get("login") or "")
        name = str(body.get("name") or "")
        existing = await db.scalar(
            select(Repo).where(
                Repo.org_id == access.org.id,
                Repo.github_repo_id == github_repo_id,
                Repo.deleted_at.is_(None),
            )
        )
        if existing is not None:
            raise ConflictError(
                f"{existing.full_name} is already connected.", code="repo_already_connected"
            )
        # Always a new row: one disconnected earlier keeps waiting for its purge, so nothing of
        # its snapshots, jobs or settings comes back.
        repo = Repo(org_id=access.org.id, github_repo_id=github_repo_id)
        repo.slug = await self._slug_for(db, access, owner, name)
        repo.source = source
        repo.installation_id = facts.installation.id if facts.installation else None
        repo.owner = owner[:100]
        repo.name = name[:100]
        repo.full_name = str(body.get("full_name"))[:201]
        repo.default_branch = str(body.get("default_branch") or "main")[:255]
        repo.visibility = str(
            body.get("visibility") or ("private" if body.get("private") else "public")
        )[:16]
        repo.size_kb = facts.size_kb
        repo.languages = facts.languages
        repo.status = RepoStatus.PENDING
        repo.settings = RepoSettings().model_dump()
        repo.connected_by = access.user.id
        try:
            # A concurrent connect of the same repository (or slug) loses at the unique index.
            async with db.begin_nested():
                db.add(repo)
                await db.flush()
        except IntegrityError as exc:
            raise ConflictError(
                "That repository was just connected.", code="repo_already_connected"
            ) from exc
        await db.refresh(repo)  # timestamps set by the database
        await audit(
            db,
            AuditAction.REPO_CONNECTED,
            actor_user_id=access.user.id,
            org_id=access.org.id,
            target_type="repo",
            target_id=repo.id,
            meta={
                "full_name": repo.full_name,
                "source": source.value,
                "visibility": repo.visibility,
            },
        )
        return repo

    # ------------------------------------------------------------------ connected repositories

    async def list(self, db: AsyncSession, access: OrgAccess) -> list[Repo]:
        rows = await db.scalars(
            select(Repo)
            .where(Repo.org_id == access.org.id, Repo.deleted_at.is_(None))
            .order_by(Repo.full_name)
        )
        return list(rows)

    async def get(self, db: AsyncSession, access: OrgAccess, slug: str) -> Repo | None:
        return await db.scalar(
            select(Repo).where(
                Repo.org_id == access.org.id,
                Repo.slug == slug.lower(),
                Repo.deleted_at.is_(None),
            )
        )

    async def update_settings(
        self, db: AsyncSession, access: OrgAccess, repo: Repo, changes: dict[str, Any]
    ) -> Repo:
        require_role(access, Role.ADMIN)
        try:
            merged = RepoSettings.model_validate({**(repo.settings or {}), **changes})
        except ValidationError as exc:
            problems = [str(error["msg"]) for error in exc.errors()]
            raise InvalidError(
                "Those settings aren't valid.",
                code="invalid_settings",
                details={"problems": problems},
            ) from None
        before = repo.settings
        repo.settings = merged.model_dump()
        await db.flush()
        await db.refresh(repo)
        await audit(
            db,
            AuditAction.REPO_SETTINGS_UPDATE,
            actor_user_id=access.user.id,
            org_id=access.org.id,
            target_type="repo",
            target_id=repo.id,
            meta={"before": before, "after": repo.settings},
        )
        return repo

    async def disconnect(self, db: AsyncSession, access: OrgAccess, repo: Repo) -> uuid.UUID:
        """Hide the repository at once; its data is purged in the background."""
        require_role(access, Role.ADMIN)
        repo.deleted_at = _now()
        await db.flush()
        await audit(
            db,
            AuditAction.REPO_DISCONNECTED,
            actor_user_id=access.user.id,
            org_id=access.org.id,
            target_type="repo",
            target_id=repo.id,
            meta={"full_name": repo.full_name},
        )
        return repo.id
