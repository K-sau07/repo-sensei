"""Feature maps (4.9): the modules mapped, the model's picks held to the candidates it was
given, the maps stored with their symbols' lines, found by path or words, stale when their
files change, and read by the agent's tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import FeatureMap, RepoFile
from sensei_engine.agent.tools import ToolContext, run_tool
from sensei_engine.llm import feature_maps
from sensei_engine.llm.feature_maps import FeatureMapDraft, Picked, modules, render
from sensei_engine.llm.summaries import FileSummary, SymbolSummary
from sensei_testing.anthropic import FakeAnthropic, message
from sensei_testing.indexed import ITEMS, ROUTE, SERVICE, index_repo

CREATE = "py backend/app/services/items.py/create_item()."
ROUTE_KEY = "py backend/app/api/routes/items.py/create_item_route()."


def _summary(path: str, symbols: dict[str, str]) -> FileSummary:
    return FileSummary(
        path,
        f"What {path} does.",
        {k: SymbolSummary(k, v, ()) for k, v in symbols.items()},
        "v1",
        "m",
    )


SUMMARIES = {
    ROUTE: _summary(ROUTE, {ROUTE_KEY: "The route that creates an item."}),
    SERVICE: _summary(SERVICE, {CREATE: "Adds a new item to the session."}),
}


def test_modules_are_the_directories_with_files_enough_largest_first() -> None:
    paths = ["a/x.py", "a/b/y.py", "a/b/z.py", "c/w.py", "top.py"]
    assert modules(paths) == [
        ("a", ["a/b/y.py", "a/b/z.py", "a/x.py"]),
        ("a/b", ["a/b/y.py", "a/b/z.py"]),
    ]
    assert modules(["one.py", "two.py"]) == [(".", ["one.py", "two.py"])]  # the root, alone
    assert modules(["one.py"]) == []


def test_the_model_picks_among_the_candidates_it_is_given() -> None:
    prompt, candidates = render("backend/app", "Items.", list(SUMMARIES.values()))
    assert candidates == [ROUTE_KEY, CREATE]
    assert "  s2 create_item: Adds a new item to the session." in prompt.splitlines()
    picks = [
        feature_maps.ModelPick(id="s2", why="saves"),
        feature_maps.ModelPick(id="S2", why="again"),
        feature_maps.ModelPick(id="s9", why="no such"),
        feature_maps.ModelPick(id="create_item", why="not a number"),
    ]
    assert feature_maps.picked(picks, candidates) == (Picked(CREATE, "saves"),)


async def test_a_map_is_written_for_each_module() -> None:
    answer = {
        "title": "Item creation",
        "summary": "Creates items.",
        "entry_points": [{"id": "s1", "why": "POST /items/"}],
        "key_symbols": [{"id": "s2", "why": "saves it"}],
        "data_flow": ["The route takes the item.", "The service adds it."],
    }

    def reply(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=message(json.dumps(answer), model="claude-haiku-5-5"))

    fake = FakeAnthropic(routes={"/v1/messages": reply})
    drafts = await feature_maps.generate(fake.client(), SUMMARIES, {"backend/app": "Items."})
    keys = [d.key for d in drafts]
    assert keys == ["dir:backend", "dir:backend/app"]
    app = drafts[1]
    assert (app.title, app.entry_points, app.key_symbols) == (
        "Item creation", (Picked(ROUTE_KEY, "POST /items/"),), (Picked(CREATE, "saves it"),),
    )  # fmt: skip
    body = fake.bodies("/v1/messages")[1]
    assert body["output_config"]["format"]["type"] == "json_schema"


# ---------------------------------------------------------------- stored


async def _ctx(db: AsyncSession, root: Path) -> ToolContext:
    snapshot, graph = await index_repo(db, root, ITEMS)
    return ToolContext(
        db=db,
        org_id=snapshot.org_id,
        repo_id=snapshot.repo_id,
        snapshot_id=snapshot.id,
        repo="o/items",
        sha="b" * 40,
        root=root,
        tree=sorted(ITEMS),
        graph=graph,
    )


DRAFT = FeatureMapDraft(
    "dir:backend/app",
    "backend/app",
    "Item creation",
    "Creates items.",
    (Picked(ROUTE_KEY, "POST /items/"),),
    (Picked(CREATE, "saves it"), Picked("py nowhere.py/gone().", "not indexed")),
    ("The route takes the item.",),
    (ROUTE, SERVICE),
)


async def test_maps_are_stored_with_their_lines_and_found(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    ctx = await _ctx(db_session, tmp_path)
    ids: dict[str, Any] = {
        "org_id": ctx.org_id,
        "repo_id": ctx.repo_id,
        "snapshot_id": ctx.snapshot_id,
    }
    assert await feature_maps.store(db_session, drafts=[DRAFT], **ids) == 1
    (stored,) = list(await db_session.scalars(select(FeatureMap)))
    assert [e["symbol_key"] for e in stored.key_symbols] == [CREATE]  # one not indexed: left out
    assert stored.entry_points[0] == {
        "symbol_key": ROUTE_KEY,
        "why": "POST /items/",
        "path": ROUTE,
        "start_line": 4,
        "end_line": 7,
    }
    assert set(stored.file_hashes) == {ROUTE, SERVICE}
    assert await feature_maps.find(db_session, ctx.snapshot_id, SERVICE) == [stored]
    assert await feature_maps.find(db_session, ctx.snapshot_id, "item creation") == [stored]
    assert await feature_maps.find(db_session, ctx.snapshot_id, "billing") == []
    assert await feature_maps.count(db_session, ctx.snapshot_id) == 1
    assert await feature_maps.current(db_session, stored)
    await db_session.execute(
        update(RepoFile)
        .where(RepoFile.snapshot_id == ctx.snapshot_id, RepoFile.path == SERVICE)
        .values(content_hash=b"\\x00" * 32)
    )
    assert not await feature_maps.current(db_session, stored)
    assert stored.stale

    shown = await run_tool(ctx, "get_feature_map", {"feature_or_path": "backend/app"})
    text = "\\n".join(b["text"] for b in shown.content if b["type"] == "text")
    assert text.startswith("Item creation (backend/app/)")
    assert "Its files changed since" in text
    assert [s.path for s in shown.sources] == [ROUTE]  # the entry point's lines, citable


async def test_an_incremental_run_keeps_a_map_whose_files_are_as_they_were(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A module's map is kept (5.2) when the module has the same files, with the same content;
    any file changed, added or gone, and it is written again."""
    ctx = await _ctx(db_session, tmp_path)
    ids: dict[str, Any] = {
        "org_id": ctx.org_id,
        "repo_id": ctx.repo_id,
        "snapshot_id": ctx.snapshot_id,
    }
    await feature_maps.store(db_session, drafts=[DRAFT], **ids)
    module = [("backend/app", [ROUTE, SERVICE])]
    (same,) = await feature_maps.kept(db_session, ctx.snapshot_id, ctx.snapshot_id, module)
    assert same.path == "backend/app"
    more = [("backend/app", [ROUTE, SERVICE, "backend/app/core/long.py"])]
    assert await feature_maps.kept(db_session, ctx.snapshot_id, ctx.snapshot_id, more) == []
    await db_session.execute(
        update(RepoFile)
        .where(RepoFile.snapshot_id == ctx.snapshot_id, RepoFile.path == SERVICE)
        .values(content_hash=b"\x00" * 32)
    )
    assert await feature_maps.kept(db_session, ctx.snapshot_id, ctx.snapshot_id, module) == []
