"""Chunk embeddings in the chunk store and semantic search over them: the chunks still to embed,
vectors written back, the partition's HNSW index (built once, by the system role only, used by
the search), and the nearest chunks of one snapshot and view, other repositories in the same
partition left out."""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from sensei_core.config import Settings
from sensei_core.mail import MemoryMailer
from sensei_core.models import CodeChunk, Membership, Org, Repo, RepoSource, Snapshot, User
from sensei_core.models.chunks import CHUNK_PARTITIONS, embedding_index
from sensei_core.orgs.service import OrgService
from sensei_engine.chunk.files import chunk_file
from sensei_engine.chunk.store import FileChunks, save_chunks
from sensei_engine.embed import store
from sensei_engine.embed.provider import EmbeddingUsage, InputType, UsageSink
from sensei_engine.ingest.discover import discover
from sensei_engine.ingest.store import save_discovery
from sensei_engine.search.bm25 import Filters, chunk_partition
from sensei_engine.search.dense import (
    DenseHit,
    DenseRetriever,
    Leg,
    dense_search,
    fuse,
    nearest_query,
    tune,
)
from sensei_testing import make_user

pytestmark = pytest.mark.integration

DIMS = 1024
FILES = {
    "app/items.py": "def list_items():\n    return []\n\n\ndef delete_item(item_id):\n    pass\n",
    "app/users.py": "def create_user(name):\n    return name\n",
    "README.md": "# Shop\n\nItems and users.\n",
}
AXES = {"README.md": 0, "app/items.py": 1, "app/users.py": 2}  # one chunk each


def axis(index: int, *, lean: int | None = None) -> list[float]:
    """A unit vector along ``index`` (leaning a little towards ``lean``)."""
    vector = [0.0] * DIMS
    vector[index] = 1.0
    if lean is not None:
        vector[lean] = 0.3
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


async def _repo(
    db: AsyncSession, org_id: uuid.UUID | None = None, *, repo_id: uuid.UUID | None = None
) -> Snapshot:
    if org_id is None:
        owner = await make_user(db)
        access = await OrgService(MemoryMailer(), "https://app.example.com").create(
            db, owner, name="Acme", slug=f"acme-{owner.github_id}"
        )
        org_id = access.org.id
    repo = Repo(
        id=repo_id or uuid.uuid4(),
        org_id=org_id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=uuid.uuid4().int % 1_000_000_000,
        owner="o",
        name=f"r{uuid.uuid4().hex[:6]}",
        full_name="o/r",
        slug=f"r{uuid.uuid4().hex[:6]}",
        default_branch="main",
        visibility="public",
    )
    db.add(repo)
    await db.flush()
    snapshot = Snapshot(org_id=org_id, repo_id=repo.id, ref="main", commit_sha="a" * 40)
    db.add(snapshot)
    await db.flush()
    return snapshot


async def _chunked(db: AsyncSession, root: Path, snapshot: Snapshot) -> list[CodeChunk]:
    for path, content in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    discovery = discover(root, max_files=100)
    await save_discovery(db, snapshot, discovery)
    file_ids = {f.path: f.id for f in discovery.files}
    chunks = [
        FileChunks(f.path, f.language, chunk_file(f.path, f.language, (root / f.path).read_bytes()))
        for f in discovery.files
    ]
    snapshot.chunk_count = await save_chunks(db, snapshot, file_ids, chunks)
    rows = await db.scalars(
        select(CodeChunk)
        .where(CodeChunk.snapshot_id == snapshot.id)
        .order_by(CodeChunk.path, CodeChunk.start_line)
    )
    return list(rows)


async def _embedded(
    db: AsyncSession, root: Path, snapshot: Snapshot | None = None
) -> tuple[Snapshot, dict[str, uuid.UUID]]:
    """FILES chunked and each chunk embedded along its file's axis; chunk ids by path."""
    snapshot = snapshot or await _repo(db)
    rows = await _chunked(db, root, snapshot)
    assert sorted(row.path for row in rows) == sorted(AXES)
    vectors = {row.id: axis(AXES[row.path]) for row in rows}
    assert await store.save(db, snapshot.repo_id, vectors) == len(rows)
    await db.flush()
    return snapshot, {row.path: row.id for row in rows}


async def test_pending_chunks_are_embedded_as_hashed_and_saved(
    system_session: AsyncSession, tmp_path: Path
) -> None:
    db = system_session
    snapshot = await _repo(db)
    rows = await _chunked(db, tmp_path, snapshot)
    assert await store.count_pending(db, snapshot.id) == len(rows) == 3
    first = await store.pending(db, snapshot.id, limit=2)
    rest = await store.pending(db, snapshot.id, after=first[-1].id, limit=2)
    found = [*first, *rest]
    assert sorted(p.id for p in found) == sorted(r.id for r in rows)
    for chunk in found:
        assert hashlib.sha256(chunk.text.encode()).digest() == chunk.content_hash
    await store.save(db, snapshot.repo_id, {found[0].id: axis(0)})
    assert await store.count_pending(db, snapshot.id) == 2
    assert found[0].id not in [p.id for p in await store.pending(db, snapshot.id, limit=10)]
    saved = await db.scalar(select(CodeChunk.embedding).where(CodeChunk.id == found[0].id))
    assert saved is not None
    assert saved[0] == 1.0


async def test_the_nearest_chunks_of_the_snapshot_and_view_come_first(
    system_session: AsyncSession, tmp_path: Path
) -> None:
    db = system_session
    snapshot, ids = await _embedded(db, tmp_path)
    rid, sid = snapshot.repo_id, snapshot.id
    # Near users.py, leaning towards items.py.
    hits = await dense_search(db, repo_id=rid, snapshot_id=sid, vector=axis(2, lean=1), k=10)
    assert [h.path for h in hits] == ["app/users.py", "app/items.py", "README.md"]
    assert hits[0].chunk_id == ids["app/users.py"]
    assert hits[0].score == pytest.approx(1 / math.sqrt(1.09), abs=1e-3)
    assert hits[2].score == pytest.approx(0.0, abs=1e-3)
    assert [h.legs for h in hits] == [{"sem_code": 1}, {"sem_code": 2}, {"sem_code": 3}]
    assert [
        h.path for h in await dense_search(db, repo_id=rid, snapshot_id=sid, vector=axis(2), k=1)
    ] == ["app/users.py"]
    docs = await dense_search(
        db, repo_id=rid, snapshot_id=sid, vector=axis(2), filters=Filters(kinds=["doc"])
    )
    assert [h.path for h in docs] == ["README.md"]
    under = await dense_search(
        db, repo_id=rid, snapshot_id=sid, vector=axis(0), filters=Filters(path_prefix="app/")
    )
    assert {h.path for h in under} == {"app/items.py", "app/users.py"}
    # No summary-view chunks yet: the summary leg finds nothing.
    assert (
        await dense_search(db, repo_id=rid, snapshot_id=sid, vector=axis(2), leg=Leg.SUMMARY) == []
    )
    assert await dense_search(db, repo_id=rid, snapshot_id=sid, vector=[], k=5) == []


async def test_another_repository_in_the_same_partition_stays_out(
    system_session: AsyncSession, tmp_path: Path
) -> None:
    db = system_session
    snapshot, _ = await _embedded(db, tmp_path / "a")
    partition = await chunk_partition(db, snapshot.repo_id)
    assert partition is not None
    remainder = int(partition.rpartition(".")[2].removeprefix("chunks_p"))
    neighbour_id = uuid.uuid4()
    while not await db.scalar(
        text("SELECT satisfies_hash_partition('chunks'::regclass, :n, :r, CAST(:id AS uuid))"),
        {"n": CHUNK_PARTITIONS, "r": remainder, "id": str(neighbour_id)},
    ):
        neighbour_id = uuid.uuid4()
    neighbour = await _repo(db, snapshot.org_id, repo_id=neighbour_id)
    await _embedded(db, tmp_path / "b", neighbour)
    assert await chunk_partition(db, neighbour_id) == partition
    hits = await dense_search(
        db, repo_id=snapshot.repo_id, snapshot_id=snapshot.id, vector=axis(0), k=10
    )
    assert len(hits) == 3
    chunk_ids = [h.chunk_id for h in hits]
    owners = await db.scalars(select(CodeChunk.repo_id).where(CodeChunk.id.in_(chunk_ids)))
    assert set(owners) == {snapshot.repo_id}


async def test_a_partitions_index_is_built_once_and_searches_use_it(
    system_session: AsyncSession, tmp_path: Path
) -> None:
    db = system_session
    snapshot, _ = await _embedded(db, tmp_path)
    partition = await chunk_partition(db, snapshot.repo_id)
    assert partition is not None
    rid, sid = snapshot.repo_id, snapshot.id
    exact = await dense_search(db, repo_id=rid, snapshot_id=sid, vector=axis(2, lean=0), k=3)

    # Another test's index run may have built (and committed) this partition's index.
    had = await db.scalar(
        text("SELECT to_regclass(:name) IS NOT NULL"), {"name": embedding_index(partition)}
    )
    built = await store.build_index(db, partition)
    already = await store.build_index(db, partition)
    assert (built, already) == (not had, False)
    definition = await db.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
        {"name": embedding_index(partition.rpartition(".")[2])},
    )
    assert "USING hnsw (embedding halfvec_cosine_ops)" in str(definition)
    assert "m='16'" in str(definition)
    assert "ef_construction='128'" in str(definition)

    assert (
        await dense_search(db, repo_id=rid, snapshot_id=sid, vector=axis(2, lean=0), k=3) == exact
    )


@pytest.fixture
async def committed(
    migrated_test_db: Settings, tmp_path: Path
) -> AsyncGenerator[tuple[Snapshot, AsyncEngine]]:
    """An embedded snapshot committed by the system role; its org, its owner and the partition
    index this test builds are removed afterwards."""
    system = create_async_engine(migrated_test_db.database.system_url.get_secret_value())
    owner = create_async_engine(migrated_test_db.database.migrate_url.get_secret_value())
    async with AsyncSession(system, expire_on_commit=False) as db:
        snapshot, _ = await _embedded(db, tmp_path)
        users = list(
            await db.scalars(select(Membership.user_id).where(Membership.org_id == snapshot.org_id))
        )
        partition = await chunk_partition(db, snapshot.repo_id)
        had_index = await db.scalar(
            text("SELECT to_regclass(:name) IS NOT NULL"), {"name": embedding_index(str(partition))}
        )
        await db.commit()
    try:
        yield snapshot, system
    finally:
        async with system.begin() as conn:
            await conn.execute(delete(Org).where(Org.id == snapshot.org_id))
            await conn.execute(delete(User).where(User.id.in_(users)))
        if not had_index:
            async with owner.begin() as conn:
                await conn.execute(text(f"DROP INDEX IF EXISTS {embedding_index(str(partition))}"))
        await system.dispose()
        await owner.dispose()


async def test_searches_walk_the_partitions_index_once_it_is_committed(
    committed: tuple[Snapshot, AsyncEngine],
) -> None:
    snapshot, system = committed
    rid, sid = snapshot.repo_id, snapshot.id
    async with AsyncSession(system) as db, db.begin():
        partition = await chunk_partition(db, rid)
        assert partition is not None
        await store.build_index(db, partition)
    async with AsyncSession(system) as db, db.begin():
        statement, params = nearest_query(
            partition, repo_id=rid, snapshot_id=sid, vector=axis(2, lean=0), k=3
        )
        await tune(db)
        # Three rows: sorting the snapshot's rows exactly is cheaper, and the planner rightly
        # takes that. Rule sorting out to see the index's path.
        await db.execute(text("SET LOCAL enable_sort = off"))
        plan = "\n".join(
            str(line) for line in await db.scalars(text(f"EXPLAIN {statement.text}"), params)
        )
        name = partition.rpartition(".")[2]  # as EXPLAIN shows it
        assert f"Index Scan using {embedding_index(name)} on {name} c" in plan
        assert await db.scalar(text("SHOW hnsw.iterative_scan")) == "relaxed_order"
        assert await db.scalar(text("SHOW hnsw.ef_search")) == "120"
        hits = await dense_search(db, repo_id=rid, snapshot_id=sid, vector=axis(2, lean=0), k=3)
    assert [h.path for h in hits] == ["app/users.py", "README.md", "app/items.py"]


async def test_only_the_system_role_builds_indexes_and_only_on_partitions(
    db_session: AsyncSession, system_session: AsyncSession
) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("SELECT chunk_embedding_index('chunks_p00')"))
    with pytest.raises(DBAPIError, match="not a partition of chunks"):
        await system_session.execute(text("SELECT chunk_embedding_index('files')"))
    with pytest.raises(ValueError, match="not a partition"):
        await store.build_index(system_session, "chunks_p99")


class FakeProvider:
    """Embeds by keyword: a text with "user" points along users.py's axis, anything else along
    the README's."""

    provider = "fake"
    model = "fake-1"
    dims = DIMS

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], InputType]] = []

    async def embed(
        self, texts: Sequence[str], input_type: InputType, *, on_usage: UsageSink | None = None
    ) -> list[list[float]]:
        self.calls.append((list(texts), input_type))
        if on_usage is not None:
            await on_usage(EmbeddingUsage("fake", "fake-1", len(texts), 7))
        return [axis(AXES["app/users.py"] if "user" in t else 0) for t in texts]


async def test_the_retriever_embeds_the_query_as_a_query_and_fuses_the_legs(
    system_session: AsyncSession, tmp_path: Path
) -> None:
    db = system_session
    snapshot, ids = await _embedded(db, tmp_path)
    provider = FakeProvider()
    billed: list[EmbeddingUsage] = []

    async def sink(usage: EmbeddingUsage) -> None:
        billed.append(usage)

    retriever = DenseRetriever(provider)
    hits = await retriever.search(
        db, repo_id=snapshot.repo_id, snapshot_id=snapshot.id, query="who creates a user?", k=3,
        on_usage=sink,
    )  # fmt: skip
    assert provider.calls == [(["who creates a user?"], InputType.QUERY)]
    assert len(billed) == 1
    assert hits[0].chunk_id == ids["app/users.py"]
    assert hits[0].legs == {"sem_code": 1}  # the summary leg found nothing
    assert len(hits) == 3
    one = await retriever.search(
        db, repo_id=snapshot.repo_id, snapshot_id=snapshot.id, query="user", legs=[Leg.CODE]
    )
    assert one[0].score == pytest.approx(1.0, abs=1e-3)  # one leg: its own scores
    assert await retriever.search(
        db, repo_id=uuid.uuid4(), snapshot_id=snapshot.id, query="user"
    ) == []  # fmt: skip
    assert len(provider.calls) == 2  # no partition, no query embedded


def test_fusion_adds_each_legs_reciprocal_rank() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()

    def make(chunk_id: uuid.UUID, path: str) -> DenseHit:
        return DenseHit(chunk_id, "function", path, 1, 2, None, None, 0.5)

    fused = fuse({Leg.CODE: [make(a, "a"), make(b, "b")], Leg.SUMMARY: [make(b, "b")]})
    assert [h.chunk_id for h in fused] == [b, a]
    assert fused[0].legs == {"sem_code": 2, "sem_summary": 1}
    assert fused[0].score == pytest.approx(1 / 62 + 0.7 / 61, abs=1e-6)
    assert fuse({Leg.CODE: [make(a, "a"), make(b, "b")]}, k=1)[0].chunk_id == a
