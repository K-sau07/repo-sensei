"""Incremental indexing (5.2): what a snapshot keeps of the one indexed before it.

An incremental run indexes a new commit as a snapshot of its own, but reads again only what
changed. Discovery lists every file (and hashes it); a file whose path and content are what
they were in the previous snapshot is **kept**: its definitions and its chunks (their vectors
too) are carried into the new snapshot, row for row, rather than parsed, cut and embedded
again. A file that only moved (the same content under a new path, the old one gone) is
**moved**: it is read again under its new path, and its chunks take their vectors from its
chunks at the old path (moved code costs nothing to embed). Everything else (added, modified)
is read as a full run reads it.

The graph and links stages read the whole snapshot again (5.3 narrows them), from the carried
definitions and the re-read ones alike. All of it runs in the caller's transaction, with the
snapshot's org set (row-level security checks every row).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.indexing.snapshots import ref_snapshot
from sensei_core.models import SYMBOL_KINDS, Repo, RepoFile, Snapshot
from sensei_core.models.indexing import SnapshotStatus
from sensei_engine.chunk.store import COLUMNS
from sensei_engine.graph.stage import GRAPH_ATTRS


@dataclass(frozen=True, slots=True)
class Carry:
    """The new snapshot's files that keep the previous one's (each: new file id → old)."""

    previous: uuid.UUID  # the previous snapshot
    previous_sha: str  # its commit (its artifacts are kept by it)
    previous_generation: int | None  # its live chunks'
    kept: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict[uuid.UUID, uuid.UUID])
    moved: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict[uuid.UUID, uuid.UUID])


async def previous_of(db: AsyncSession, repo: Repo, snapshot: Snapshot) -> Snapshot | None:
    """The snapshot an incremental run of ``snapshot`` builds on: the repository's last index,
    when it can be read (ready, with its tree) and isn't this one."""
    if repo.last_indexed_sha is None or repo.last_indexed_sha == snapshot.commit_sha:
        return None
    previous = await db.scalar(
        select(Snapshot).where(
            Snapshot.repo_id == repo.id, Snapshot.commit_sha == repo.last_indexed_sha
        )
    )
    if previous is None or previous.status is not SnapshotStatus.READY:
        return None
    return previous if previous.merkle_root is not None else None


async def carried(db: AsyncSession, previous: Snapshot, snapshot: Snapshot) -> Carry:
    """Which of ``snapshot``'s files keep ``previous``'s facts, and which only moved."""
    columns = (RepoFile.path, RepoFile.content_hash, RepoFile.language, RepoFile.flags, RepoFile.id)

    def read(rows: Iterable[Any]) -> list[tuple[str, bytes, Read, uuid.UUID]]:
        # How a file is read: its language and flags (vendored, generated, binary...), which
        # .gitattributes can change without its content changing.
        return [
            (path, content_hash, (language, tuple(sorted(flags or ()))), file_id)
            for path, content_hash, language, flags, file_id in rows
        ]

    old_files = read(await db.execute(select(*columns).where(RepoFile.snapshot_id == previous.id)))
    new_files = read(
        await db.execute(
            select(*columns).where(RepoFile.snapshot_id == snapshot.id).order_by(RepoFile.path)
        )
    )
    old = {(path, content_hash, how): file_id for path, content_hash, how, file_id in old_files}
    kept: dict[uuid.UUID, uuid.UUID] = {}
    for path, content_hash, how, file_id in new_files:
        if (found := old.get((path, content_hash, how))) is not None:
            kept[file_id] = found
    # A file that moved: its content was at a path the new snapshot no longer has.
    present = {path for path, _, _, _ in new_files}
    gone: dict[tuple[bytes, Read], list[uuid.UUID]] = {}
    for path, content_hash, how, file_id in sorted(old_files):
        if path not in present:
            gone.setdefault((content_hash, how), []).append(file_id)
    moved: dict[uuid.UUID, uuid.UUID] = {}
    for _, content_hash, how, file_id in new_files:
        if file_id not in kept and gone.get((content_hash, how)):
            moved[file_id] = gone[(content_hash, how)].pop(0)
    return Carry(previous.id, previous.commit_sha, previous.chunk_generation, kept, moved)


Read = tuple[str | None, tuple[str, ...]]  # a file's language and flags


def _pairs(mapping: dict[uuid.UUID, uuid.UUID]) -> dict[str, list[uuid.UUID]]:
    return {"new": list(mapping), "old": list(mapping.values())}


async def carry_symbols(db: AsyncSession, snapshot: Snapshot, carry: Carry) -> int:
    """The kept files' definitions, written into ``snapshot`` as they were in the previous one,
    but for what the graph stage kept on them (:data:`~sensei_engine.graph.stage.GRAPH_ATTRS`:
    it writes those again, from this snapshot's graph). Returns the rows written."""
    if not carry.kept:
        return 0
    result = await db.execute(
        text(
            "INSERT INTO public.nodes (org_id, repo_id, snapshot_id, kind, symbol_key, name, "
            "qualified_name, file_id, start_line, end_line, start_col, end_col, lang, signature, "
            "docstring, attrs, content_hash, owner_file_id) "
            "SELECT n.org_id, n.repo_id, :snapshot, n.kind, n.symbol_key, n.name, "
            "n.qualified_name, m.new_id, n.start_line, n.end_line, n.start_col, n.end_col, n.lang, "
            "n.signature, n.docstring, n.attrs - CAST(:graph_attrs AS text[]), n.content_hash, "
            "m.new_id "
            "FROM public.nodes n "
            "JOIN unnest(CAST(:old AS uuid[]), CAST(:new AS uuid[])) AS m(old_id, new_id) "
            "ON n.owner_file_id = m.old_id "
            "WHERE n.snapshot_id = :previous AND n.kind = ANY(:kinds) "
            "ON CONFLICT (snapshot_id, symbol_key) DO NOTHING"
        ),
        {
            "snapshot": snapshot.id,
            "previous": carry.previous,
            "kinds": sorted(str(kind) for kind in SYMBOL_KINDS),
            "graph_attrs": list(GRAPH_ATTRS),
            **_pairs(carry.kept),
        },
    )
    return int(getattr(result, "rowcount", 0) or 0)


# Chunk columns carried as they are (the rest are the new snapshot's, or worked out again).
_SAME = tuple(
    c for c in COLUMNS if c not in {"id", "snapshot_id", "generation", "file_id", "owner_file_id"}
)


async def carry_chunks(
    db: AsyncSession, snapshot: Snapshot, carry: Carry, *, generation: int
) -> int:
    """The kept files' code chunks, written into ``snapshot``'s ``generation`` with their
    vectors (nothing to embed again), each pointing at its definition in ``snapshot`` (carried
    first). Summary chunks are the summaries stage's. Returns the rows written."""
    if not carry.kept or carry.previous_generation is None:
        return 0
    same = ", ".join(_SAME)
    selected = ", ".join(f"c.{column}" for column in _SAME)
    result = await db.execute(
        text(
            f"INSERT INTO public.chunks (snapshot_id, generation, file_id, owner_file_id, "  # noqa: S608 - constant names
            f"{same}, embedding, symbol_id) "
            f"SELECT :snapshot, :generation, m.new_id, m.new_id, {selected}, c.embedding, s.id "
            "FROM public.chunks c "
            "JOIN unnest(CAST(:old AS uuid[]), CAST(:new AS uuid[])) AS m(old_id, new_id) "
            "ON c.owner_file_id = m.old_id "
            "LEFT JOIN public.nodes s ON s.snapshot_id = :snapshot AND s.symbol_key = c.symbol_key "
            "WHERE c.repo_id = :repo AND c.snapshot_id = :previous "
            "AND c.generation = :previous_generation AND c.view = 'code'"
        ),
        {
            "snapshot": snapshot.id,
            "generation": generation,
            "repo": snapshot.repo_id,
            "previous": carry.previous,
            "previous_generation": carry.previous_generation,
            **_pairs(carry.kept),
        },
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def carry_moved_vectors(
    db: AsyncSession, snapshot: Snapshot, carry: Carry, *, generation: int
) -> int:
    """Each moved file's chunks (cut again under its new path) given the vector of its chunk at
    the old path (the same kind, lines, part and view): moved code costs nothing to embed.
    Returns the chunks given one."""
    if not carry.moved or carry.previous_generation is None:
        return 0
    result = await db.execute(
        text(
            "UPDATE public.chunks c SET embedding = o.embedding "
            "FROM public.chunks o, "
            "unnest(CAST(:old AS uuid[]), CAST(:new AS uuid[])) AS m(old_id, new_id) "
            "WHERE c.repo_id = :repo AND c.snapshot_id = :snapshot AND c.generation = :generation "
            "AND c.owner_file_id = m.new_id AND c.embedding IS NULL "
            "AND o.repo_id = :repo AND o.snapshot_id = :previous "
            "AND o.generation = :previous_generation AND o.owner_file_id = m.old_id "
            "AND o.embedding IS NOT NULL AND o.view = c.view AND o.kind = c.kind "
            "AND o.start_line = c.start_line AND o.end_line = c.end_line "
            "AND o.part IS NOT DISTINCT FROM c.part"
        ),
        {
            "snapshot": snapshot.id,
            "generation": generation,
            "repo": snapshot.repo_id,
            "previous": carry.previous,
            "previous_generation": carry.previous_generation,
            **_pairs(carry.moved),
        },
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def changed_paths(db: AsyncSession, snapshot: Snapshot, carry: Carry) -> set[str]:
    """The paths an incremental run reads that the last index read otherwise: added, modified
    and moved (the new paths), and deleted (and moved: the old paths)."""
    rows = await db.execute(
        select(RepoFile.id, RepoFile.path).where(RepoFile.snapshot_id == snapshot.id)
    )
    new: dict[uuid.UUID, str] = dict(rows.tuples().all())
    old = set(await db.scalars(select(RepoFile.path).where(RepoFile.snapshot_id == carry.previous)))
    kept = {new[file_id] for file_id in carry.kept if file_id in new}
    return {path for path in new.values() if path not in kept} | (old - set(new.values()))


async def moved_on(
    db: AsyncSession, repo: Repo, ref: str, snapshot: Snapshot
) -> tuple[bool, set[str] | None]:
    """Whether ``ref`` moves on in reading ``snapshot`` (it read another, or nothing), and the
    paths read otherwise than at what it read before (None: nothing to compare with). Worked
    out before the ref is pointed at it: a branch is compared with its own last index, not the
    default branch's."""
    before = await ref_snapshot(db, repo, ref)
    if before is None and ref == repo.default_branch:
        before = await previous_of(db, repo, snapshot)
    if before is None:
        return True, None
    if before.id == snapshot.id:
        return False, None  # the same commit indexed again: nothing new
    if before.status is not SnapshotStatus.READY or before.merkle_root is None:
        return True, None
    return True, await changed_paths(db, snapshot, await carried(db, before, snapshot))
