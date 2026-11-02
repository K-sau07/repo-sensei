"""Where a fix's stages read and work (8.5): a checkout of the repository with history, at the
commit being fixed, and the read tools over it.

The commit is the run's (``commit_sha``), or, when it has none yet (the investigation, its first
stage that reads code), the repository's last index. Each fix has a working copy of its own
beside the ingest cache (``fix-worktrees/<fix id>``: a later stage patches it), fetched with
:data:`HISTORY_DEPTH` commits of history so ``git_log`` and ``git_blame`` have something to
read; the token reaches git as the ingest's does (never in argv, config or logs). A machine
keeps at most :data:`MAX_WORKTREES` of them (the least recently used go first, never one a stage
is using), and a fix's goes when it ends (:func:`drop_worktree`).

What the stage needs from the database is read in a short session, and the clone happens
outside any. The tools read the checkout itself (the indexed files, at the indexed commit: the
same bytes) with the snapshot's file list, its graph and the search service, through a session
of their own whose every transaction is scoped to the org as it begins: the stages end it after
each round of tool calls (:func:`sensei_fixer.tooling.settle`), so none stays open across model
turns, and a failed query doesn't fail the ones after it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import ORG_SETTING, set_org_context
from sensei_core.errors import ConflictError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import FixRun, GitHubInstallation, Repo, RepoFile
from sensei_engine.agent.tools import ToolContext
from sensei_engine.graph.store import GraphStore
from sensei_engine.search.grep import ExactSearch
from sensei_engine.search.service import SearchService
from sensei_fixer.history import GitHistory
from sensei_fixer.stages import StageContext
from sensei_workers.ingest.cache import CacheBusyError, repo_lock
from sensei_workers.ingest.git import CloneError, CloneSource, GitRunner
from sensei_workers.runtime import get_runtime
from sensei_workers.tasks.ingest import HISTORY_SIZE_FACTOR, Target, clone_token

HISTORY_DEPTH = 200  # commits: enough for blame on what changed lately, bounded
MAX_WORKTREES = 16  # fixes' working copies a machine keeps
GRAPHS = GraphStore(max_mb=512)  # this worker's: graphs read once per snapshot
LOCKS = ".locks"


def fix_worktrees(cache_dir: Path) -> Path:
    """Where fixes' working copies go: beside the ingest cache (``<cache>/../fix-worktrees``)."""
    return cache_dir.parent / "fix-worktrees"


def _name(fix_id: uuid.UUID) -> str:
    return f"fix-{fix_id}"


def drop_worktree(cache_dir: Path, fix_id: uuid.UUID) -> None:
    """Delete a fix's working copy on this machine (it ended), unless a stage is using it."""
    folder = fix_worktrees(cache_dir)
    with contextlib.suppress(CacheBusyError), repo_lock(folder, _name(fix_id), wait=False):
        shutil.rmtree(folder / str(fix_id), ignore_errors=True)


def prune_worktrees(folder: Path, keep: uuid.UUID, limit: int = MAX_WORKTREES) -> list[str]:
    """Make room for ``keep``'s copy: delete the least recently used beyond ``limit - 1`` that
    no stage is using. What was deleted."""
    found: list[tuple[float, Path]] = []
    with contextlib.suppress(FileNotFoundError):
        for entry in os.scandir(folder):
            if entry.name in (LOCKS, str(keep)) or not entry.is_dir(follow_symlinks=False):
                continue
            found.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
    found.sort()
    dropped: list[str] = []
    for _, path in found[: max(0, len(found) - (limit - 1))]:
        try:
            with repo_lock(folder, f"fix-{path.name}", wait=False):
                shutil.rmtree(path, ignore_errors=True)
        except CacheBusyError:
            continue
        dropped.append(path.name)
    return dropped


def _org_scoped(db: AsyncSession, org_id: uuid.UUID) -> None:
    """Every transaction ``db`` begins is scoped to ``org_id`` (row-level security), so ending
    one (a rollback) leaves the next as it was."""

    def begun(_session: Any, _transaction: Any, connection: Any) -> None:
        connection.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": ORG_SETTING, "value": str(org_id)},
        )

    event.listen(db.sync_session, "after_begin", begun)


@asynccontextmanager
async def fix_workspace(ctx: StageContext) -> AsyncGenerator[tuple[ToolContext, GitHistory]]:
    runtime = get_runtime()
    settings = runtime.settings
    async with runtime.database.session() as db:
        await set_org_context(db, ctx.org_id)
        repo = await db.get(Repo, ctx.repo_id)
        run = await db.get(FixRun, ctx.fix_id)
        if repo is None or run is None:
            raise LookupError("the fix's repository is gone")
        snapshot = await resolve_snapshot(db, repo, run.commit_sha)
        installation = (
            await db.get(GitHubInstallation, repo.installation_id) if repo.installation_id else None
        )
        target = Target(
            repo_id=repo.id, org_id=repo.org_id, full_name=repo.full_name,
            ref=repo.default_branch, source=repo.source, github_repo_id=repo.github_repo_id,
            installation_id=installation.installation_id if installation else None,
            size_kb=repo.size_kb, sha=snapshot.commit_sha,
        )  # fmt: skip
        paths = list(
            await db.scalars(
                select(RepoFile.path)
                .where(RepoFile.snapshot_id == snapshot.id)
                .order_by(RepoFile.path)
            )
        )
        storage = await runtime.storage()
        try:
            graph = await GRAPHS.get(db, storage, snapshot)
        except ConflictError:  # not linked yet: the graph's tools say so, the rest work
            graph = None
        snapshot_id, sha = snapshot.id, snapshot.commit_sha
        org_id, repo_id, slug = repo.org_id, repo.id, f"{repo.owner}/{repo.name}"
    ingest = settings.ingest
    if target.size_kb * 1024 > ingest.max_checkout_bytes * HISTORY_SIZE_FACTOR:
        raise CloneError("repo_too_large", "The repository is too large to check out for a fix.")
    token = await clone_token(target)
    url = f"{settings.github.web_url.rstrip('/')}/{target.full_name}.git"
    # Beside the ingest cache, not in it: its eviction would take a copy a stage works in.
    folder = fix_worktrees(ingest.cache_dir)
    dest = folder / str(ctx.fix_id)

    def fetch() -> None:
        prune_worktrees(folder, keep=ctx.fix_id)
        GitRunner(ingest).checkout(
            CloneSource(url=url, token=token), sha, dest,
            deadline=time.monotonic() + ingest.clone_timeout_seconds, depth=HISTORY_DEPTH,
        )  # fmt: skip
        os.utime(dest)  # used now: the last to be pruned

    with repo_lock(folder, _name(ctx.fix_id)):  # held while the stage works in it
        await asyncio.to_thread(fetch)
        async with runtime.database.session() as tools_db:
            _org_scoped(tools_db, org_id)
            tools = ToolContext(
                db=tools_db, org_id=org_id, repo_id=repo_id, snapshot_id=snapshot_id,
                repo=slug, sha=sha, root=dest, tree=paths, search=SearchService(),
                exact=ExactSearch(), graph=graph,
            )  # fmt: skip
            yield tools, GitHistory(dest, sha)
