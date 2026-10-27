"""Hybrid retrieval (2.9): fusion by reciprocal rank per symbol, layer tags, and a plan's legs run
on an indexed snapshot (BM25, symbols, grep; semantic legs pending)."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import SearchSettings
from sensei_core.mail import MemoryMailer
from sensei_core.models import CodeChunk, Repo, RepoFile, RepoSource, Snapshot
from sensei_core.orgs.service import OrgService
from sensei_engine.chunk.files import chunk_file
from sensei_engine.chunk.store import FileChunks, save_chunks
from sensei_engine.ingest.discover import discover
from sensei_engine.ingest.store import save_discovery
from sensei_engine.parse.parser import parse_file
from sensei_engine.parse.stage import parseable
from sensei_engine.search.bm25 import Filters
from sensei_engine.search.hybrid import (
    ChunkRef,
    HybridRetriever,
    Ranking,
    Weights,
    WorktreeGrep,
    fuse,
)
from sensei_engine.search.layers import lane_of, layer_of
from sensei_engine.search.plan import Leg, Origin, SubQuery, plan
from sensei_engine.search.route import Lane, Layer, classify
from sensei_engine.symbols.extract import extract_symbols
from sensei_engine.symbols.store import FileSymbols, save_symbols
from sensei_testing import make_user

# ---------------------------------------------------------------------- fusion


def _sub(leg: Leg, origin: Origin = Origin.ORIGINAL) -> SubQuery:
    return SubQuery(leg, "q", origin, ())


def _ref(name: str, key: str | None = None) -> ChunkRef:
    return ChunkRef(uuid.uuid5(uuid.NAMESPACE_OID, name), key)


A1, A2 = _ref("a1", "py a.py/a()."), _ref("a2", "py a.py/a().")  # two parts of a()
B = _ref("b", "py b.py/b().")
DOC = _ref("doc")  # a Markdown section: no symbol


def test_a_symbols_chunks_are_one_result_at_its_best_rank() -> None:
    weights = Weights(legs={Leg.LEX_CODE: 1.0}, multi_hit=0.0)
    fused = fuse([Ranking(_sub(Leg.LEX_CODE), [A2, A1, B, DOC])], weights)
    assert [r.unit for r in fused] == ["py a.py/a().", "py b.py/b().", f"chunk:{DOC.chunk_id}"]
    first = fused[0]
    assert (first.legs, first.chunks) == ({"lex_code": 1}, 2)
    assert first.chunk_id == A2.chunk_id  # the part that ranked, the snippet
    # B is second, not third: a's other part took no rank.
    assert fused[1].score == pytest.approx(1.0 / 62)


def test_rankings_add_up_by_weight_and_a_second_chunk_earns_a_small_bonus() -> None:
    weights = Weights(
        legs={Leg.LEX_CODE: 1.0, Leg.SYM: 2.0}, origins={Origin.ORIGINAL: 1.0}, multi_hit=0.5
    )
    fused = fuse(
        [Ranking(_sub(Leg.LEX_CODE), [B, A1]), Ranking(_sub(Leg.SYM), [A2])],
        weights,
    )
    by_unit = {r.unit: r for r in fused}
    a, b = by_unit["py a.py/a()."], by_unit["py b.py/b()."]
    assert a.score == pytest.approx(1 / 62 + 2 / 61 + 0.5 / 61)
    assert b.score == pytest.approx(1 / 61)
    assert a.legs == {"lex_code": 2, "sym": 1}
    assert fused[0].unit == "py a.py/a()."


def test_a_ranking_weighed_zero_is_left_out_and_ties_keep_the_first_found() -> None:
    weights = Weights(legs={Leg.LEX_CODE: 1.0, Leg.GREP: 0.0})
    fused = fuse(
        [
            Ranking(_sub(Leg.LEX_CODE), [B]),
            Ranking(_sub(Leg.LEX_CODE, Origin.STRUCTURAL), [A1]),
            Ranking(_sub(Leg.GREP), [DOC]),
        ],
        Weights(legs=weights.legs, origins={Origin.ORIGINAL: 1.0, Origin.STRUCTURAL: 1.0}),
    )
    assert [r.unit for r in fused] == ["py b.py/b().", "py a.py/a()."]


def test_a_chunk_of_several_definitions_is_one_result_shown_as_the_one_found_most() -> None:
    merged = uuid.uuid5(uuid.NAMESPACE_OID, "merged")
    fused = fuse(
        [
            Ranking(_sub(Leg.LEX_CODE), [ChunkRef(merged, None)]),
            Ranking(_sub(Leg.SYM), [ChunkRef(merged, None, "py m.py/g().")]),
            Ranking(_sub(Leg.GREP), [ChunkRef(merged, None, "py m.py/f().")]),
        ],
        Weights(legs={Leg.LEX_CODE: 1.0, Leg.SYM: 1.2, Leg.GREP: 1.0}),
    )
    assert [(r.unit, r.focus) for r in fused] == [(f"chunk:{merged}", "py m.py/g().")]


def test_the_weights_are_the_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    assert Weights.from_settings(SearchSettings()) == Weights()
    monkeypatch.setenv("SENSEI_SEARCH_GREP", "0")
    monkeypatch.setenv("SENSEI_SEARCH_ORIGIN_ROUTE", "0.5")
    weights = Weights.from_settings(SearchSettings())
    assert (weights.legs[Leg.GREP], weights.origins[Origin.ROUTE]) == (0.0, 0.5)


# ---------------------------------------------------------------------- layers


@pytest.mark.parametrize(
    ("role", "kind", "path", "layer"),
    [
        ("api", "function", "backend/app/api/routes/items.py", Layer.HANDLER),
        ("api", "code", "src/backend/base/langflow/api/router.py", Layer.ROUTE),
        ("api", "function", "src/backend/base/langflow/api/router.py", Layer.HANDLER),
        ("ui", "class", "frontend/src/client/sdk.gen.ts", Layer.CLIENT),
        ("ui", "function", "src/frontend/src/controllers/API/use-post-login.ts", Layer.CLIENT),
        ("ui", "hook", "frontend/src/hooks/useAuth.ts", Layer.STATE),
        ("ui", "variable", "src/frontend/src/stores/flowStore.ts", Layer.STATE),
        ("ui", "component", "frontend/src/routes/login.tsx", Layer.UI),
        ("service", "function", "backend/app/crud.py", Layer.SERVICE),
        ("service", "function", "backend/app/workers/tasks.py", Layer.WORKER),
        ("db", "class", "backend/app/models.py", Layer.DB),
        ("test", "function", "backend/tests/test_login.py", Layer.TEST),
        ("config", "config", "docker-compose.yml", Layer.CONFIG),
        ("docs", "doc", "README.md", Layer.DOCS),
        ("other", "text", "logo.txt", None),
    ],
)
def test_hits_are_tagged_with_their_layer(
    role: str, kind: str, path: str, layer: Layer | None
) -> None:
    assert layer_of(role, kind, path) is layer


def test_layers_fold_into_lanes() -> None:
    assert [lane_of(x) for x in (Layer.CLIENT, Layer.ROUTE, Layer.WORKER, Layer.DB, None)] == [
        Lane.UI, Lane.API, Lane.SVC, Lane.DB, Lane.OTHER,
    ]  # fmt: skip


# ---------------------------------------------------------------------- on an index

FILES = {
    "backend/app/api/routes/items.py": (
        "from app.services.items import create_item\n"
        "\n"
        "\n"
        "@router.post('/items/')\n"
        "def create_item_route(item_in):\n"
        '    """Create a new item."""\n'
        "    return create_item(item_in)\n"
    ),
    "backend/app/services/items.py": (
        "def create_item(item_in):\n"
        "    item = Item(title=item_in.title)\n"
        "    session.add(item)\n"
        "    return item\n"
    ),
    "backend/tests/test_items.py": (
        "def test_create_item():\n    assert create_item(ItemIn(title='x')).title == 'x'\n"
    ),
    "README.md": "# Items\n\nCreate an item with POST /items/.\n",
}


async def _index(db: AsyncSession, root: Path, files: dict[str, str] | None = None) -> Snapshot:
    for path, content in (files or FILES).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), "https://app.example.com").create(
        db, owner, name="Acme", slug=f"acme-{owner.github_id}"
    )
    repo = Repo(
        org_id=access.org.id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=11,
        owner="o",
        name="items",
        full_name="o/items",
        slug="items",
        default_branch="main",
        visibility="public",
    )
    db.add(repo)
    await db.flush()
    snapshot = Snapshot(org_id=repo.org_id, repo_id=repo.id, ref="main", commit_sha="b" * 40)
    db.add(snapshot)
    await db.flush()
    discovery = discover(root, max_files=100)
    await save_discovery(db, snapshot, discovery)
    file_ids = {f.path: f.id for f in discovery.files}
    symbols: list[FileSymbols] = []
    chunks: list[FileChunks] = []
    for found in discovery.files:
        data = (root / found.path).read_bytes()
        parsed = found_symbols = None
        if parseable(found.language) and found.language is not None:
            parsed = parse_file(found.path, data, language=found.language)
            found_symbols = extract_symbols(parsed, found.language)
            symbols.append(FileSymbols(found.path, found.language, found_symbols))
        made = chunk_file(found.path, found.language, data, parsed=parsed, symbols=found_symbols)
        chunks.append(FileChunks(found.path, found.language, made))
    await save_symbols(db, snapshot, file_ids, symbols)
    await save_chunks(db, snapshot, file_ids, chunks)
    await db.flush()
    return snapshot


async def test_a_question_finds_definitions_tagged_by_layer(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _index(db_session, tmp_path)
    found = plan(classify("how is a new item created?").route)
    result = await HybridRetriever(grep=WorktreeGrep(tmp_path)).search(
        db_session, found, repo_id=snapshot.repo_id, snapshot_id=snapshot.id, k=10
    )
    assert result.pending == (Leg.SEM_CODE, Leg.SEM_SUMMARY)  # no embeddings yet
    by_key = {hit.symbol_key: hit for hit in result.hits}
    service = by_key["py backend/app/services/items.py/create_item()."]
    assert (service.name, service.kind, service.layer, service.lane) == (
        "create_item", "function", Layer.SERVICE, Lane.SVC,
    )  # fmt: skip
    assert (service.start_line, service.end_line, service.language) == (1, 4, "python")
    handler = by_key["py backend/app/api/routes/items.py/create_item_route()."]
    assert (handler.layer, handler.lane) == (Layer.HANDLER, Lane.API)
    scores = [hit.score for hit in result.hits]
    assert scores == sorted(scores, reverse=True)
    assert len({hit.symbol_key or hit.chunk_id for hit in result.hits}) == len(result.hits)


async def test_symbol_and_grep_legs_find_what_words_miss(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _index(db_session, tmp_path)
    retriever = HybridRetriever(grep=WorktreeGrep(tmp_path))
    # A symbol query: its short-circuit runs the symbol lookup alone.
    found = plan(classify("where is create_item_route defined?").route)
    result = await retriever.search(
        db_session, found, repo_id=snapshot.repo_id, snapshot_id=snapshot.id
    )
    assert [r.sub.leg for r in result.rankings] == [Leg.SYM]
    route = result.hits[0]
    assert route.symbol_key == "py backend/app/api/routes/items.py/create_item_route()."
    # The file is one chunk of its small definitions: the hit is the one found, in it.
    assert (route.name, route.kind, route.layer) == ("create_item_route", "function", Layer.HANDLER)
    assert (route.start_line, route.end_line, route.chunk_start, route.chunk_end) == (4, 7, 1, 7)
    # A literal: grep finds the line, reported as the definition around it.
    found = plan(classify('where is "session.add(item)" used').route)
    result = await retriever.search(
        db_session, found, repo_id=snapshot.repo_id, snapshot_id=snapshot.id
    )
    assert Leg.GREP in {r.sub.leg for r in result.rankings}
    assert result.hits[0].symbol_key == "py backend/app/services/items.py/create_item()."
    assert result.hits[0].legs.get("grep") == 1


async def test_excluded_layers_and_filters_hold_on_every_leg(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _index(db_session, tmp_path)
    retriever = HybridRetriever(grep=WorktreeGrep(tmp_path))
    everything = await retriever.search(
        db_session,
        plan(classify("create_item").route),
        repo_id=snapshot.repo_id,
        snapshot_id=snapshot.id,
    )
    assert any(hit.layer is Layer.TEST for hit in everything.hits)
    no_tests = await retriever.search(
        db_session,
        plan(classify("create_item (not tests)").route),
        repo_id=snapshot.repo_id,
        snapshot_id=snapshot.id,
    )
    assert no_tests.hits
    assert all(hit.layer is not Layer.TEST for hit in no_tests.hits)
    backend = await retriever.search(
        db_session,
        plan(classify("create_item").route),
        filters=Filters(path_prefix="backend/app/api/"),
        repo_id=snapshot.repo_id,
        snapshot_id=snapshot.id,
    )
    assert backend.hits
    assert {hit.path for hit in backend.hits} == {"backend/app/api/routes/items.py"}


async def test_every_leg_reads_the_generation_live_when_the_search_began(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _index(db_session, tmp_path)
    before = set(
        await db_session.scalars(select(CodeChunk.id).where(CodeChunk.snapshot_id == snapshot.id))
    )
    grep = WorktreeGrep(tmp_path)

    async def grep_after_a_run_went_live(
        sub: SubQuery, limit: int, filters: Filters
    ) -> list[tuple[str, int]]:
        # Another run of the chunks stage finishes between the lexical legs and this one.
        file_ids = {f.path: f.id for f in await db_session.scalars(
            select(RepoFile).where(RepoFile.snapshot_id == snapshot.id)
        )}  # fmt: skip
        files = [FileChunks(p, None, chunk_file(p, None, c)) for p, c in FILES.items()]
        await save_chunks(db_session, snapshot, file_ids, files)
        return await grep(sub, limit, filters)

    found = dataclasses.replace(
        plan(classify("create item").route),
        legs=(
            SubQuery(Leg.LEX_CODE, "create item", Origin.ORIGINAL, ()),
            SubQuery(Leg.GREP, "session.add(item)", Origin.ORIGINAL, ()),
        ),
    )
    rankings, _ = await HybridRetriever(grep=grep_after_a_run_went_live).rankings(
        db_session, found, repo_id=snapshot.repo_id, snapshot_id=snapshot.id
    )
    assert {Leg.LEX_CODE, Leg.GREP} <= {r.sub.leg for r in rankings}
    read = {ref.chunk_id for ranking in rankings for ref in ranking.found}
    assert read
    assert read <= before


async def test_a_pattern_narrowed_to_a_path_finds_its_lines_there(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    # More matching lines before the prefix (in path order) than a grep leg reads.
    files = {f"apps/a{n:02}.py": "SECRET_TOKEN = 1\n" * 5 for n in range(60)}
    files["backend/auth.py"] = "def check(token):\n    return token == SECRET_TOKEN\n"
    snapshot = await _index(db_session, tmp_path, files)
    found = plan(classify("SECRET_\\w+").route)
    assert {sub.leg for sub in found.legs} == {Leg.GREP}
    result = await HybridRetriever(grep=WorktreeGrep(tmp_path)).search(
        db_session,
        found,
        repo_id=snapshot.repo_id,
        snapshot_id=snapshot.id,
        filters=Filters(path_prefix="backend/"),
    )
    assert [hit.path for hit in result.hits] == ["backend/auth.py"]


async def test_symbols_found_outside_the_filters_are_left_out(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _index(db_session, tmp_path)
    repo_id, snapshot_id = snapshot.repo_id, snapshot.id
    found = plan(classify("create_item").route)
    retriever = HybridRetriever()
    everywhere = await retriever.search(db_session, found, repo_id=repo_id, snapshot_id=snapshot_id)
    assert "backend/tests/test_items.py" in {hit.path for hit in everywhere.hits}
    scoped = await retriever.search(
        db_session,
        found,
        repo_id=repo_id,
        snapshot_id=snapshot_id,
        filters=Filters(path_prefix="backend/app/"),
    )
    assert scoped.hits
    assert {hit.path for hit in scoped.hits} <= {
        "backend/app/services/items.py",
        "backend/app/api/routes/items.py",
    }
    by_role = await retriever.search(
        db_session,
        found,
        repo_id=repo_id,
        snapshot_id=snapshot_id,
        filters=Filters(roles=["test"]),
    )
    assert {hit.path for hit in by_role.hits} == {"backend/tests/test_items.py"}
    # Filtered where the symbols are located: nothing outside them takes a rank.
    rankings, _ = await retriever.rankings(
        db_session,
        found,
        repo_id=repo_id,
        snapshot_id=snapshot_id,
        filters=Filters(roles=["test"]),
    )
    ranked = [ref.chunk_id for ranking in rankings for ref in ranking.found]
    paths = await db_session.scalars(select(CodeChunk.path).where(CodeChunk.id.in_(ranked)))
    assert set(paths) == {"backend/tests/test_items.py"}


async def test_a_snapshot_not_chunked_finds_nothing(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    snapshot = await _index(db_session, tmp_path)
    snapshot.chunk_generation = None
    await db_session.flush()
    result = await HybridRetriever().search(
        db_session,
        plan(classify("create_item").route),
        repo_id=snapshot.repo_id,
        snapshot_id=snapshot.id,
    )
    assert (result.hits, result.rankings) == ([], [])
