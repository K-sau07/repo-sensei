"""What an incremental run keeps (5.2): a file is kept only if it is read as it was (the same
path, content, language and flags), so a file .gitattributes now marks vendored isn't carried."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import RepoFile, Snapshot
from sensei_engine.incremental import carried
from sensei_testing.indexed import ITEMS, ROUTE, SERVICE, index_repo


@pytest.mark.integration
async def test_a_file_read_otherwise_now_is_not_kept(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    previous, _ = await index_repo(db_session, tmp_path, ITEMS)
    snapshot = Snapshot(
        org_id=previous.org_id, repo_id=previous.repo_id, ref="main", commit_sha="c" * 40
    )
    db_session.add(snapshot)
    await db_session.flush()
    columns = [c.name for c in RepoFile.__table__.columns if c.name not in ("id", "snapshot_id")]
    listed = ", ".join(columns)
    await db_session.execute(
        text(
            f"INSERT INTO files (snapshot_id, {listed}) "
            f"SELECT :snapshot, {listed} FROM files WHERE snapshot_id = :previous"
        ),
        {"snapshot": snapshot.id, "previous": previous.id},
    )
    # The same content, but now vendored (a .gitattributes change).
    await db_session.execute(
        text("UPDATE files SET flags = ARRAY['vendored'] WHERE snapshot_id = :s AND path = :p"),
        {"s": snapshot.id, "p": SERVICE},
    )
    carry = await carried(db_session, previous, snapshot)
    rows = await db_session.execute(
        select(RepoFile.id, RepoFile.path).where(RepoFile.snapshot_id == snapshot.id)
    )
    paths: dict[object, str] = dict(rows.tuples().all())
    kept = {paths[file_id] for file_id in carry.kept}
    assert ROUTE in kept
    assert SERVICE not in kept  # read otherwise now: read again, not carried
    assert carry.moved == {}
