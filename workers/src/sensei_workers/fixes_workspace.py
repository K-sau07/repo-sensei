"""Where a fix's stages read and work (8.5): a checkout of the repository with history, at the
commit being fixed, and the read tools over it.

The commit is the run's (``commit_sha``), or, when it has none yet (the investigation, its first
stage that reads code), the repository's last index. Each fix has a working copy of its own
beside the ingest cache (``fix-worktrees/<fix id>``: a later stage patches it), fetched with
:data:`HISTORY_DEPTH` commits of history so ``git_log`` and ``git_blame`` have something to
read; the token reaches git as the ingest's does (never in argv, config or logs).

The tools read the checkout itself (the indexed files, at the indexed commit: the same bytes)
with the snapshot's file list, its graph and the search service; the session stays open while
the stage works (the tools query it).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import select

from sensei_core.db.tenancy import set_org_context
from sensei_core.errors import ConflictError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import FixRun, GitHubInstallation, Repo, RepoFile
from sensei_engine.agent.tools import ToolContext
from sensei_engine.graph.store import GraphStore
from sensei_engine.search.grep import ExactSearch
from sensei_engine.search.service import SearchService
from sensei_fixer.history import GitHistory
from sensei_fixer.stages import StageContext
from sensei_workers.ingest.cache import repo_lock
from sensei_workers.ingest.git import CloneSource, GitRunner
from sensei_workers.runtime import get_runtime
from sensei_workers.tasks.ingest import Target, clone_token

HISTORY_DEPTH = 200  # commits: enough for blame on what changed lately, bounded
GRAPHS = GraphStore(max_mb=512)  # this worker's: graphs read once per snapshot


def fix_worktrees(cache_dir: Path) -> Path:
    """Where fixes' working copies go: beside the ingest cache (``<cache>/../fix-worktrees``)."""
    return cache_dir.parent / "fix-worktrees"


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
        token = await clone_token(target)
        url = f"{settings.github.web_url.rstrip('/')}/{repo.full_name}.git"
        # Beside the ingest cache, not in it: its eviction would take a copy a stage works in.
        dest = fix_worktrees(settings.ingest.cache_dir) / str(ctx.fix_id)
        name = f"fix-{ctx.fix_id}"

        def fetch() -> None:
            with repo_lock(dest.parent, name):
                GitRunner(settings.ingest).checkout(
                    CloneSource(url=url, token=token), snapshot.commit_sha, dest,
                    deadline=time.monotonic() + settings.ingest.clone_timeout_seconds,
                    depth=HISTORY_DEPTH,
                )  # fmt: skip

        await asyncio.to_thread(fetch)
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
        tools = ToolContext(
            db=db, org_id=repo.org_id, repo_id=repo.id, snapshot_id=snapshot.id,
            repo=f"{repo.owner}/{repo.name}", sha=snapshot.commit_sha, root=dest, tree=paths,
            search=SearchService(), exact=ExactSearch(),
            graph=graph,
        )  # fmt: skip
        yield tools, GitHistory(dest, snapshot.commit_sha)
