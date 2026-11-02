"""Fixes' working copies (8.5): a machine keeps a bounded number (the least recently used go,
never one a stage is using), a fix's goes when it ends, and the tools' session is scoped to the
org in every transaction it begins (so ending one after each round of calls is safe)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from sensei_core.config import Settings
from sensei_workers.fixes_workspace import (
    _org_scoped,  # pyright: ignore[reportPrivateUsage]
    drop_worktree,
    fix_worktrees,
    prune_worktrees,
)
from sensei_workers.ingest.cache import repo_lock


def _copies(folder: Path, n: int) -> list[uuid.UUID]:
    ids = [uuid.UUID(int=i + 1) for i in range(n)]
    for age, fix_id in enumerate(ids):
        (folder / str(fix_id)).mkdir(parents=True)
        (folder / str(fix_id) / "README.md").write_text("x")
        os.utime(folder / str(fix_id), (1_000 + age, 1_000 + age))  # the first is the oldest
    return ids


def test_the_least_recently_used_copies_go_but_never_one_in_use(tmp_path: Path) -> None:
    folder = tmp_path / "fix-worktrees"
    ids = _copies(folder, 5)
    new = uuid.UUID(int=99)
    with repo_lock(folder, f"fix-{ids[0]}"):  # a stage works in the oldest
        dropped = prune_worktrees(folder, keep=new, limit=3)
    # Room for the new one: two kept. The oldest is in use, so the next two go.
    assert dropped == [str(ids[1]), str(ids[2])]
    assert sorted(p.name for p in folder.iterdir() if p.name != ".locks") == sorted(
        [str(ids[0]), str(ids[3]), str(ids[4])]
    )


def test_a_fix_that_ended_leaves_no_copy(tmp_path: Path) -> None:
    cache = tmp_path / "repos"
    folder = fix_worktrees(cache)
    [fix_id] = _copies(folder, 1)
    with repo_lock(folder, f"fix-{fix_id}"):
        drop_worktree(cache, fix_id)  # in use: stays
    assert (folder / str(fix_id)).exists()
    drop_worktree(cache, fix_id)
    assert not (folder / str(fix_id)).exists()
    drop_worktree(cache, uuid.uuid4())  # none: nothing to do


async def test_every_transaction_the_tools_begin_is_the_orgs(migrated_test_db: Settings) -> None:
    engine = create_async_engine(migrated_test_db.database.url.get_secret_value())
    org = uuid.uuid4()
    try:
        async with AsyncSession(engine) as db:
            _org_scoped(db, org)
            query = text("SELECT current_setting('app.org_id', true)")
            assert await db.scalar(query) == str(org)
            await db.rollback()  # a round of tool calls ended (or a query failed)
            assert not db.in_transaction()
            assert await db.scalar(query) == str(org)
    finally:
        await engine.dispose()
