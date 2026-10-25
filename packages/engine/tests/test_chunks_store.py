"""Saving chunks (COPY through a staging table under row-level security, linked to their
nodes), the partitions and who may touch them, and BM25 keyword search over the
fastapi-template fixture."""

from __future__ import annotations

import tarfile
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from sensei_core.config import Settings
from sensei_core.db.tenancy import set_org_context
from sensei_core.mail import MemoryMailer
from sensei_core.models import CodeChunk, CodeNode, Repo, RepoSource, Snapshot
from sensei_core.models.chunks import CHUNK_PARTITIONS, bm25_index, chunk_partitions
from sensei_core.orgs.service import OrgService
from sensei_engine.chunk.files import chunk_file
from sensei_engine.chunk.store import ChunkWriter, FileChunks, save_chunks
from sensei_engine.ingest.discover import discover
from sensei_engine.ingest.store import save_discovery
from sensei_engine.parse.parser import parse_file
from sensei_engine.parse.stage import parseable
from sensei_engine.search.bm25 import (
    Filters,
    Leg,
    LexHit,
    bm25_query,
    bm25_search,
    chunk_partition,
    keyword_search,
    searchable_partition,
)
from sensei_engine.symbols.extract import extract_symbols
from sensei_engine.symbols.store import FileSymbols, save_symbols
from sensei_testing import make_user

pytestmark = pytest.mark.integration

ARCHIVE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"
FILES = {
    "app/models.py": (
        "class Item:\n"
        "    def save(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def make_item():\n"
        "    return Item()\n"
    ),
    "README.md": "# Items\n\nHow items work.\n",
}


async def _snapshot(db: AsyncSession) -> Snapshot:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name="Acme", slug=f"acme-{owner.github_id}"
    )
    repo = Repo(
        org_id=access.org.id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=7,
        owner="o",
        name="r",
        full_name="o/r",
        slug="r",
        default_branch="main",
        visibility="public",
    )
    db.add(repo)
    await db.flush()
    snapshot = Snapshot(org_id=repo.org_id, repo_id=repo.id, ref="main", commit_sha="a" * 40)
    db.add(snapshot)
    await db.flush()
    return snapshot


async def _index(
    db: AsyncSession, root: Path, files: dict[str, str] | None = None
) -> tuple[Snapshot, dict[str, uuid.UUID]]:
    """``root`` (written from ``files`` if given) indexed as the pipeline does: files, symbols,
    then chunks."""
    for path, content in (files or {}).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    snapshot = await _snapshot(db)
    discovery = discover(root, max_files=1000)
    await save_discovery(db, snapshot, discovery)
    file_ids = {f.path: f.id for f in discovery.files}
    symbols: list[FileSymbols] = []
    chunks: list[FileChunks] = []
    for found in discovery.files:
        if "binary" in found.flags or "too_large" in found.flags:
            continue
        data = (root / found.path).read_bytes()
        result = None
        found_symbols = None
        if parseable(found.language) and found.language is not None:
            result = parse_file(found.path, data, language=found.language)
            found_symbols = extract_symbols(result, found.language)
            symbols.append(FileSymbols(found.path, found.language, found_symbols))
        made = chunk_file(found.path, found.language, data, parsed=result, symbols=found_symbols)
        chunks.append(FileChunks(found.path, found.language, made))
    snapshot.symbol_count = await save_symbols(db, snapshot, file_ids, symbols)
    snapshot.chunk_count = await save_chunks(db, snapshot, file_ids, chunks)
    await db.flush()
    return snapshot, file_ids


async def test_chunks_are_saved_and_linked_to_their_nodes(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot, file_ids = await _index(db_session, tmp_path, FILES)
    rows = (
        await db_session.scalars(select(CodeChunk).where(CodeChunk.snapshot_id == snapshot.id))
    ).all()
    rows = sorted(rows, key=lambda r: (r.path, r.start_line))
    assert snapshot.chunk_count == len(rows)
    assert [(r.path, r.kind, r.start_line, r.end_line, r.symbol_key) for r in rows] == [
        ("README.md", "doc", 1, 3, None),
        ("app/models.py", "class", 1, 3, "py app/models.py/Item#"),
        ("app/models.py", "function", 6, 7, "py app/models.py/make_item()."),
    ]
    item = rows[1]
    node = await db_session.scalar(
        select(CodeNode).where(CodeNode.symbol_key == "py app/models.py/Item#")
    )
    assert node is not None
    assert item.symbol_id == node.id
    assert rows[0].symbol_id is None
    assert (item.org_id, item.repo_id, item.file_id, item.owner_file_id) == (
        snapshot.org_id,
        snapshot.repo_id,
        file_ids["app/models.py"],
        file_ids["app/models.py"],
    )
    assert (item.view, item.lang, item.part, item.parts, item.embedding) == (
        "code",
        "python",
        None,
        None,
        None,
    )
    assert item.content.startswith("class Item:")
    assert item.header.startswith("# path: app/models.py")
    assert item.tokens > 0
    assert len(item.content_hash) == 32


async def test_saving_again_replaces_and_incremental_saves_replace_their_files(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot, file_ids = await _index(db_session, tmp_path, FILES)

    async def count(path: str | None = None) -> int:
        query = select(func.count()).select_from(CodeChunk)
        query = query.where(CodeChunk.snapshot_id == snapshot.id)
        if path is not None:
            query = query.where(CodeChunk.path == path)
        return int(await db_session.scalar(query) or 0)

    total = await count()
    readme = chunk_file("README.md", "markdown", "# Items\n\n## More\n\nText.\n")
    await save_chunks(
        db_session,
        snapshot,
        file_ids,
        [FileChunks("README.md", "markdown", readme)],
        replace_all=False,
    )
    assert await count("README.md") == len(readme)
    assert await count() == total - 1 + len(readme)
    # A full save replaces everything; files not in the snapshot are passed over.
    written = await save_chunks(
        db_session,
        snapshot,
        file_ids,
        [FileChunks("README.md", "markdown", readme), FileChunks("nope.md", "markdown", readme)],
    )
    assert written == await count() == len(readme)


async def test_chunks_are_written_in_batches(db_session: AsyncSession, tmp_path: Path) -> None:
    snapshot, file_ids = await _index(db_session, tmp_path, FILES)
    chunks = chunk_file("app/models.py", "python", FILES["app/models.py"])
    writer = ChunkWriter(db_session, snapshot, file_ids, batch_rows=1)
    for _ in range(3):  # the same file thrice: each batch is flushed on its own
        await writer.add(FileChunks("app/models.py", "python", chunks))
    assert writer.written == 3 * len(chunks)
    assert await writer.finish() == 3 * len(chunks)


async def test_chunks_are_org_scoped(db_session: AsyncSession, tmp_path: Path) -> None:
    await _index(db_session, tmp_path, FILES)
    await set_org_context(db_session, uuid.uuid4())  # another org
    assert (await db_session.scalars(select(CodeChunk))).all() == []


# ---------------------------------------------------------------------- partitions and roles


@pytest.fixture
async def engines(migrated_test_db: Settings) -> AsyncGenerator[dict[str, AsyncEngine]]:
    database = migrated_test_db.database
    found = {
        "app": create_async_engine(database.url.get_secret_value()),
        "system": create_async_engine(database.system_url.get_secret_value()),
        "owner": create_async_engine(database.migrate_url.get_secret_value()),
    }
    yield found
    for engine in found.values():
        await engine.dispose()


async def test_every_partition_has_its_bm25_indexes(engines: dict[str, AsyncEngine]) -> None:
    async with engines["owner"].connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT ix.relname AS name FROM pg_inherits i "
                "JOIN pg_index x ON x.indrelid = i.inhrelid "
                "JOIN pg_class ix ON ix.oid = x.indexrelid JOIN pg_am am ON am.oid = ix.relam "
                "WHERE i.inhparent = 'chunks'::regclass AND am.amname = 'bm25'"
            )
        )
        found = sorted(str(row.name) for row in rows)
    expected = sorted(
        bm25_index(partition, column)
        for partition in chunk_partitions()
        for column in ("search_text", "path_text")
    )
    assert found == expected
    assert len(chunk_partitions()) == CHUNK_PARTITIONS


async def test_a_partition_read_directly_is_org_scoped_too(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot, _ = await _index(db_session, tmp_path, FILES)
    partition = await chunk_partition(db_session, snapshot.repo_id)
    assert partition in chunk_partitions()
    query = text(f"SELECT count(*) FROM {partition} WHERE snapshot_id = :s")
    assert await db_session.scalar(query, {"s": snapshot.id}) == snapshot.chunk_count
    await set_org_context(db_session, uuid.uuid4())  # another org
    assert await db_session.scalar(query, {"s": snapshot.id}) == 0
    assert await chunk_partition(db_session, snapshot.repo_id) is None


@pytest.mark.parametrize("table", ["nodes", "chunks", "chunks_p07"])
async def test_the_system_role_may_analyze_and_the_app_role_may_not(
    engines: dict[str, AsyncEngine], table: str
) -> None:
    """ANALYZE by a role that may not maintain a table only warns and skips it, so the
    privilege itself is checked; the system role's ANALYZE runs (the worker's, after loads)."""
    check = text("SELECT has_table_privilege(current_user, :table, 'MAINTAIN')")
    async with engines["system"].connect() as connection:
        assert await connection.scalar(check, {"table": table}) is True
        await connection.execute(text(f"ANALYZE {table}"))
    async with engines["app"].connect() as connection:
        assert await connection.scalar(check, {"table": table}) is False


# ---------------------------------------------------------------------- BM25


@pytest.fixture
async def fixture_snapshot(
    db_session: AsyncSession, tmp_path: Path
) -> tuple[AsyncSession, Snapshot]:
    with tarfile.open(ARCHIVE) as archive:
        archive.extractall(tmp_path, filter="data")
    snapshot, _ = await _index(db_session, tmp_path / "fastapi-template")
    return db_session, snapshot


async def _search(
    db: AsyncSession,
    snapshot: Snapshot,
    query: str,
    leg: Leg = Leg.CODE,
    filters: Filters = Filters(),  # noqa: B008
) -> list[tuple[str, str | None]]:
    hits = await bm25_search(
        db,
        repo_id=snapshot.repo_id,
        snapshot_id=snapshot.id,
        query=query,
        leg=leg,
        k=10,
        filters=filters,
    )
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(score > 0 for score in scores)
    return [(h.path, h.symbol_key) for h in hits]


def test_the_query_gets_its_identifiers_words() -> None:
    assert bm25_query("getUserById") == "getuserbyid get user by id"
    assert bm25_query("where is read_items?") == "where is readitems read items"
    assert bm25_query("  ") == ""


async def test_an_exact_identifier_ranks_above_noise(
    fixture_snapshot: tuple[AsyncSession, Snapshot],
) -> None:
    db, snapshot = fixture_snapshot
    # read_items and the client's readItems are one term (readitems): both come first.
    found = await _search(db, snapshot, "read_items")
    assert {key for _, key in found[:2]} == {
        "py backend/app/api/routes/items.py/read_items().",
        "ts frontend/src/client/sdk.gen.ts/ItemsService#readItems().",
    }
    found = await _search(db, snapshot, "UpdatePassword")
    assert found[0] == ("backend/app/models.py", "py backend/app/models.py/UpdatePassword#")
    # Words of an identifier find it too.
    found = await _search(db, snapshot, "verify password hash")
    assert found[0][0] == "backend/app/core/security.py"


async def test_the_path_leg_matches_paths_signatures_and_names(
    fixture_snapshot: tuple[AsyncSession, Snapshot],
) -> None:
    db, snapshot = fixture_snapshot
    found = await _search(db, snapshot, "items routes", Leg.PATH)
    assert {path for path, _ in found[:5]} == {
        "backend/app/api/routes/items.py",
        "frontend/src/routes/_layout/items.tsx",
    }
    found = await _search(db, snapshot, "UpdatePassword", Leg.PATH)
    assert found[0] == ("backend/app/models.py", "py backend/app/models.py/UpdatePassword#")


async def test_filters_apply_before_the_limit(
    fixture_snapshot: tuple[AsyncSession, Snapshot],
) -> None:
    db, snapshot = fixture_snapshot
    tsx = await _search(db, snapshot, "items", filters=Filters(languages=["tsx"]))
    assert tsx
    assert all(path.endswith(".tsx") for path, _ in tsx)
    backend = await _search(db, snapshot, "items", filters=Filters(path_prefix="backend/"))
    assert all(path.startswith("backend/") for path, _ in backend)
    tests = await _search(db, snapshot, "items", filters=Filters(roles=["test"]))
    assert tests
    assert all("test" in path for path, _ in tests)
    assert {path for path, _ in tests} != {path for path, _ in backend}
    classes = await _search(db, snapshot, "user", filters=Filters(kinds=["class"]))
    assert classes
    assert all(key is not None and key.endswith("#") for _, key in classes)
    assert await _search(db, snapshot, "xyzzyqq") == []


async def test_the_legs_fuse_by_reciprocal_rank(
    fixture_snapshot: tuple[AsyncSession, Snapshot],
) -> None:
    db, snapshot = fixture_snapshot
    hits = await keyword_search(
        db, repo_id=snapshot.repo_id, snapshot_id=snapshot.id, query="read_items", k=10
    )
    assert {hit.symbol_key for hit in hits[:2]} == {
        "py backend/app/api/routes/items.py/read_items().",
        "ts frontend/src/client/sdk.gen.ts/ItemsService#readItems().",
    }
    assert set(hits[0].legs) == {"lex_code", "lex_path"}
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    other = await keyword_search(
        db, repo_id=snapshot.repo_id, snapshot_id=uuid.uuid4(), query="read_items"
    )
    assert other == []


async def test_a_snapshot_is_searched_only_once_chunked(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot, _ = await _index(db_session, tmp_path, FILES)
    repo_id, snapshot_id = snapshot.repo_id, snapshot.id

    async def found() -> tuple[list[LexHit], list[LexHit]]:
        fused = await keyword_search(
            db_session, repo_id=repo_id, snapshot_id=snapshot_id, query="make_item"
        )
        leg = await bm25_search(
            db_session, repo_id=repo_id, snapshot_id=snapshot_id, query="make_item"
        )
        return fused, leg

    assert all(await found())
    assert await searchable_partition(db_session, repo_id, snapshot_id) is not None
    # Discovered again (a reindex of the commit): its files' chunks are gone with them, and
    # it waits for the chunks stage.
    await save_discovery(db_session, snapshot, discover(tmp_path, max_files=1000))
    await db_session.flush()
    assert snapshot.chunk_count is None
    assert await searchable_partition(db_session, repo_id, snapshot_id) is None
    assert await found() == ([], [])
