"""The agent's tools (4.1): their definitions (strict, byte-stable), and each tool on a small
indexed repository: citable blocks, pages, errors that say how to call, size caps, and no
writes."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_engine.agent.overview import repo_overview
from sensei_engine.agent.tools import TOOLS, CitationIndex, ToolContext, definitions, run_tool
from sensei_engine.agent.tools.base import (
    MAX_RESULT_TOKENS,
    MAX_STRICT_OPTIONAL,
    Results,
    ToolResult,
    args_key,
    block_spans,
    code_result,
    next_cursor,
    optional_parameters,
)
from sensei_engine.chunk.tokens import count_tokens
from sensei_testing.indexed import ITEMS, ROUTE, SERVICE, index_repo

SNAPSHOT = Path(__file__).parent / "fixtures" / "agent_tools.json"
FILES = ITEMS


# ---------------------------------------------------------------- definitions


def _check(schema: Mapping[str, Any], value: object, where: str) -> None:
    """``value`` against the subset of JSON Schema the tools use (a strict schema's)."""
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float)}
    kind = schema["type"]
    if kind == "integer":
        assert isinstance(value, int), where
        assert not isinstance(value, bool), where
    else:
        assert isinstance(value, kinds[kind]), where
    if "enum" in schema:
        assert value in schema["enum"], where
    if isinstance(value, dict):
        given = cast("dict[str, object]", value)
        assert set(schema["required"]) <= set(given), where
        assert set(given) <= set(schema["properties"]), where
        for key, item in given.items():
            _check(schema["properties"][key], item, f"{where}.{key}")
    if isinstance(value, list):
        for n, item in enumerate(cast("list[object]", value)):
            _check(schema["items"], item, f"{where}[{n}]")


def _strict(schema: Mapping[str, Any], where: str) -> None:
    if schema["type"] == "object":
        assert schema["additionalProperties"] is False, where
        assert set(schema["required"]) <= set(schema["properties"]), where
        for key, inner in schema["properties"].items():
            _strict(inner, f"{where}.{key}")
    if schema["type"] == "array":
        _strict(schema["items"], f"{where}[]")


async def test_files_are_found_whatever_order_the_tree_comes_in(tmp_path: Path) -> None:
    # As en_US collation orders them (punctuation ignored at first): not as bisection needs.
    collated = ["app/alembic/env.py", "app/alembic.ini", "app/alembic_utils.py"]
    for path in collated:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(f"# {path}\n")
    ctx = ToolContext(
        db=cast("AsyncSession", None), org_id=uuid4(), repo_id=uuid4(), snapshot_id=uuid4(),
        repo="o/r", sha="a" * 40, root=tmp_path, tree=collated,
    )  # fmt: skip
    assert list(ctx.tree) == sorted(collated)
    for path in collated:
        done = await run_tool(ctx, "read_code", {"path": path})
        assert not done.is_error, (path, done.summary)


def test_a_result_with_search_results_holds_only_them_and_its_notes_follow() -> None:
    code: dict[str, Any] = {"type": "search_result", "source": "a.py", "title": "a", "content": []}
    note = {"type": "text", "text": "2 more results were left out."}
    mixed = ToolResult([note, code], [], "found")
    assert mixed.param("t1")["content"] == [code]
    assert mixed.notes("code_search") == [
        {"type": "text", "text": "(About the code_search result.) 2 more results were left out."}
    ]
    plain = ToolResult([note], [], "nothing")
    assert (plain.param("t2")["content"], plain.notes("grep")) == ([note], [])


def test_the_strict_tools_keep_within_the_apis_optional_parameters() -> None:
    strict = [tool for tool in TOOLS if tool.strict]
    assert sum(optional_parameters(tool.schema) for tool in strict) <= MAX_STRICT_OPTIONAL
    assert [tool.name for tool in TOOLS if not tool.strict] == ["code_search", "grep"]


def test_every_tool_is_closed_described_and_its_examples_valid() -> None:
    found = definitions()
    assert [d["name"] for d in found] == [
        "code_search", "grep", "find_symbol", "graph_neighbors", "trace_feature", "read_code",
        "list_dir", "get_feature_map", "submit_evidence",
    ]  # fmt: skip
    for definition in found:
        name = definition["name"]
        assert definition["strict"] is (name not in {"code_search", "grep"})
        assert definition["description"].count(". ") >= 3, name  # when to use it, and when not
        _strict(definition["input_schema"], name)
        assert definition["input_examples"], name
        for example in definition["input_examples"]:
            _check(definition["input_schema"], example, name)


def test_the_definitions_are_byte_stable() -> None:
    """The tools are the cached prefix's first bytes: any change to them is a change to this
    snapshot (regenerate it with ``SENSEI_SNAPSHOT=1``), never a side effect."""
    rendered = json.dumps(definitions(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("SENSEI_SNAPSHOT") == "1":
        SNAPSHOT.write_text(rendered)
    assert SNAPSHOT.read_text() == rendered
    assert json.dumps(definitions()) == json.dumps(definitions())  # the same bytes every call


# ---------------------------------------------------------------- blocks and the index


def test_blocks_hold_five_to_twenty_lines() -> None:
    assert block_spans(1, 3) == [(1, 3)]  # shorter than five: all there is
    assert block_spans(1, 12) == [(1, 12)]
    assert block_spans(10, 25) == [(10, 21), (22, 34)]  # a last block of 1 joins the one before
    assert block_spans(1, 29) == [(1, 12), (13, 24), (25, 29)]
    for count in range(5, 200):
        spans = block_spans(1, count)
        assert all(5 <= end - start + 1 <= 20 for start, end in spans), count
        assert spans[0][0] == 1
        assert spans[-1][1] == count
        assert all(b[0] == a[1] + 1 for a, b in pairwise(spans))


def _context(root: Path, **overrides: Any) -> ToolContext:
    fields: dict[str, Any] = {
        "db": None,
        "org_id": None,
        "repo_id": None,
        "snapshot_id": None,
        "repo": "o/items",
        "sha": "b" * 40,
        "root": root,
        "tree": sorted(FILES),
    }
    return ToolContext(**{**fields, **overrides})


def test_a_citation_resolves_to_the_lines_of_the_blocks_it_cites(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    lines = [f"line {n}" for n in range(1, 31)]
    block, source = code_result(ctx, "a.py", 1, lines, title="a.py")
    assert block["source"] == f"o/items@{'b' * 40}:a.py#L1-L30"
    assert block["citations"] == {"enabled": True}
    assert block["content"][0]["text"].splitlines()[0] == "1\tline 1"
    index = CitationIndex()
    index.add([code_result(ctx, "b.py", 5, ["x"] * 5)[1], source])
    cited = index.resolve(1, 1, 3)  # the second result's blocks 1 and 2
    assert cited is not None
    assert (cited.path, cited.start_line, cited.end_line) == ("a.py", 13, 30)
    assert index.resolve(2, 0, 1) is None  # no such result
    assert index.resolve(1, 3, 4) is None  # no such block
    assert index.resolve(1, 2, 2) is None  # no blocks at all


def test_what_does_not_fit_is_left_out_and_said(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    out = Results()
    lines = [f"value_{n} = {'y' * 300}" for n in range(60)]
    kept = sum(out.code(*code_result(ctx, f"f{n}.py", 1, lines)) for n in range(20))
    result = out.done("x", hits=20, narrow="narrow with path_glob")
    assert 0 < kept < 20
    assert _tokens(result) <= MAX_RESULT_TOKENS
    assert result.content[-1]["text"] == (
        f"{20 - kept} more results were left out to keep this answer short; narrow with path_glob."
    )


def _tokens(result: ToolResult) -> int:
    """The text a result shows the model: its texts, and each search result's source, title
    and lines."""
    texts: list[str] = []
    for block in result.content:
        if block["type"] == "text":
            texts.append(block["text"])
        else:
            texts += [block["source"], block["title"], *(b["text"] for b in block["content"])]
    return sum(count_tokens(text) for text in texts)


# ---------------------------------------------------------------- on an index


@pytest.fixture
async def ctx(db_session: AsyncSession, tmp_path: Path) -> ToolContext:
    snapshot, graph = await index_repo(db_session, tmp_path, FILES)
    return _context(
        tmp_path,
        db=db_session,
        org_id=snapshot.org_id,
        repo_id=snapshot.repo_id,
        snapshot_id=snapshot.id,
        graph=graph,
    )


def _text(result: ToolResult) -> str:
    return "\n".join(
        block["text"] if block["type"] == "text" else block["title"] for block in result.content
    )


def _sources(result: ToolResult) -> list[str]:
    return [b["source"] for b in result.content if b["type"] == "search_result"]


async def test_code_search_gives_citable_definitions(ctx: ToolContext) -> None:
    result = await run_tool(ctx, "code_search", {"query": "create item", "mode": "keyword"})
    assert not result.is_error, _text(result)
    titles = [b["title"] for b in result.content if b["type"] == "search_result"]
    assert f"sym:{SERVICE}::create_item (function, svc)" in titles
    assert len(result.sources) == len(titles)
    assert all(s.startswith(f"o/items@{'b' * 40}:") for s in _sources(result))
    layered = await run_tool(
        ctx, "code_search", {"query": "create item", "mode": "keyword", "layer": "svc"}
    )
    assert {s.path for s in layered.sources} == {SERVICE}
    globbed = await run_tool(
        ctx, "code_search", {"query": "create item", "mode": "keyword", "path_glob": "**/routes/*"}
    )
    assert {s.path for s in globbed.sources} == {ROUTE}


async def test_grep_pages_and_a_cursor_is_only_good_for_its_own_call(ctx: ToolContext) -> None:
    first = await run_tool(ctx, "grep", {"pattern": "item", "limit": 2})
    assert not first.is_error, _text(first)
    note = first.content[-1]["text"]
    assert note.startswith("Showing 1-2 of")
    cursor = note.split("next_cursor: ")[1].split(" ")[0]
    second = await run_tool(ctx, "grep", {"pattern": "item", "limit": 2, "cursor": cursor})
    assert second.content[-1]["text"].startswith("Showing 3-4 of")
    other = await run_tool(ctx, "grep", {"pattern": "session", "limit": 2, "cursor": cursor})
    assert other.is_error
    assert "belongs to another call" in _text(other)
    found = await run_tool(ctx, "grep", {"pattern": "session.add"})
    assert [(s.path, s.blocks) for s in found.sources] == [(SERVICE, ((1, 4),))]  # 2 around 3


async def test_find_symbol_gives_ids_the_other_tools_take(ctx: ToolContext) -> None:
    found = await run_tool(ctx, "find_symbol", {"name": "create_item"})
    text = _text(found)
    assert text.splitlines()[0].startswith(f"1. sym:{SERVICE}::create_item  (function)")
    detailed = await run_tool(
        ctx, "find_symbol", {"name": "create_item_route", "response_format": "detailed"}
    )
    assert detailed.sources[0].path == ROUTE  # then near misses (create_item)
    neighbours = await run_tool(
        ctx, "graph_neighbors", {"symbol": f"sym:{ROUTE}::create_item_route"}
    )
    assert not neighbours.is_error, _text(neighbours)
    assert "→ calls" in _text(neighbours)
    assert f"sym:{SERVICE}::create_item" in _text(neighbours)
    callers = await run_tool(
        ctx, "graph_neighbors", {"symbol": f"sym:{SERVICE}::create_item", "direction": "in"}
    )
    assert "← calls" in _text(callers)


async def test_symbol_and_graph_tools_leave_cards_of_what_they_found(ctx: ToolContext) -> None:
    found = await run_tool(ctx, "find_symbol", {"name": "create_item"})
    assert found.card is not None
    assert found.card["kind"] == "symbols"
    first = found.card["symbols"][0]
    assert (first["name"], first["path"]) == ("create_item", SERVICE)
    assert first["symbolKey"]  # the index's key, as the symbols and graph APIs take it
    assert first["lines"][0] >= 1
    neighbours = await run_tool(
        ctx, "graph_neighbors", {"symbol": f"sym:{ROUTE}::create_item_route"}
    )
    card = neighbours.card
    assert card is not None
    assert card["kind"] == "graph"
    assert (card["root"]["name"], card["root"]["path"]) == ("create_item_route", ROUTE)
    # As the graph API sends a hop, so the page's mini graph draws it as it is.
    hop = next(h for h in card["hops"] if h["node"]["name"] == "create_item")
    assert set(hop) == {"node", "edge", "depth", "via"}
    assert (hop["edge"]["type"], hop["edge"]["direction"]) == ("calls", "out")
    missing = await run_tool(ctx, "find_symbol", {"name": "nothing_named_this"})
    assert missing.card is None


async def test_trace_feature_lists_each_hop(ctx: ToolContext) -> None:
    traced = await run_tool(ctx, "trace_feature", {"entry": f"sym:{ROUTE}::create_item_route"})
    assert not traced.is_error, _text(traced)
    text = _text(traced)
    assert text.startswith(f"From sym:{ROUTE}::create_item_route.")
    assert f"sym:{SERVICE}::create_item" in text
    assert traced.card is not None
    (path, *_) = traced.card["paths"]
    names = [(hop["node"]["name"], hop["node"]["path"]) for hop in path["hops"]]
    assert names[:2] == [("create_item_route", ROUTE), ("create_item", SERVICE)]
    assert path["hops"][0]["edge"] is None  # the entry
    assert path["hops"][1]["edge"]["type"] == "calls"


async def test_read_code_reads_lines_and_cuts_a_long_read(ctx: ToolContext) -> None:
    read = await run_tool(ctx, "read_code", {"path": SERVICE, "start_line": 2, "end_line": 3})
    assert read.content[0]["content"][0]["text"] == (
        "2\t    item = Item(title=item_in.title)\n3\t    session.add(item)"
    )
    assert _text(read).endswith(f"{SERVICE} goes on to line 4.")
    long = await run_tool(ctx, "read_code", {"path": "backend/app/core/long.py"})
    assert _tokens(long) <= MAX_RESULT_TOKENS
    assert "Cut at line" in _text(long)
    past = await run_tool(ctx, "read_code", {"path": SERVICE, "start_line": 9})
    assert past.is_error
    assert "has 4 lines" in _text(past)
    missing = await run_tool(ctx, "read_code", {"path": "backend/nope.py"})
    assert missing.is_error
    assert "For example: read_code(" in _text(missing)
    unsafe = await run_tool(ctx, "read_code", {"path": "../etc/passwd"})
    assert unsafe.is_error
    assert "not a path in the repository" in _text(unsafe)


async def test_list_dir_lists_directories_with_their_counts(ctx: ToolContext) -> None:
    root = _text(await run_tool(ctx, "list_dir", {}))
    assert root.splitlines()[:3] == ["(root)/  (5 files)", "backend/  (4 files)", "README.md"]
    deep = _text(await run_tool(ctx, "list_dir", {"path": "backend/app", "depth": 2}))
    assert "  routes/  (1 files)" in deep.splitlines()
    missing = await run_tool(ctx, "list_dir", {"path": "frontend"})
    assert missing.is_error


async def test_submit_evidence_keeps_what_exists(ctx: ToolContext) -> None:
    result = await run_tool(
        ctx,
        "submit_evidence",
        {
            "candidates": [
                {"target": f"sym:{SERVICE}::create_item", "layer": "svc", "why": "saves it"},
                {"target": ROUTE, "start_line": 4, "end_line": 7, "layer": "api", "why": "route"},
                {"target": SERVICE, "start_line": 3, "end_line": 40, "layer": "svc", "why": "x"},
                {"target": "sym:backend/app/nope.py::gone", "layer": "svc", "why": "x"},
            ]
        },
    )
    assert [(e.path, e.start_line, e.end_line) for e in ctx.evidence] == [
        (SERVICE, 1, 4), (ROUTE, 4, 7),
    ]  # fmt: skip
    text = _text(result)
    assert "Recorded 2 pieces of evidence (2 in all)." in text
    assert f"3. {SERVICE}: lines 3-40 aren't in {SERVICE} (4 lines)." in text
    assert "4. Nothing in backend/app/nope.py at this commit is named gone." in text


async def test_errors_say_how_to_call(ctx: ToolContext) -> None:
    unknown = await run_tool(ctx, "run_shell", {})
    assert unknown.is_error
    assert "the tools are code_search, grep" in _text(unknown)
    wrong = await run_tool(ctx, "read_code", {"path": SERVICE, "start_line": "ten"})
    assert wrong.is_error
    assert "wrong type" in _text(wrong)
    bad_kind = await run_tool(ctx, "find_symbol", {"name": "x", "kind": "macro"})
    assert bad_kind.is_error
    assert "For example: find_symbol(" in _text(bad_kind)
    feature_map = await run_tool(ctx, "get_feature_map", {"feature_or_path": "items"})
    assert not feature_map.is_error
    assert "No feature map covers" in _text(feature_map)
    foreign = await run_tool(ctx, "grep", {"pattern": "x", "cursor": next_cursor("other", 5)})
    assert foreign.is_error


async def test_no_tool_writes(ctx: ToolContext, db_session: AsyncSession) -> None:
    connection = await db_session.connection()
    written: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        if statement.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            written.append(statement)

    event.listen(connection.sync_connection, "before_cursor_execute", record)
    try:
        for tool in TOOLS:
            for example in tool.examples:
                await run_tool(ctx, tool.name, example)
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", record)
    assert written == []


async def test_the_overview_says_what_the_repository_holds(ctx: ToolContext) -> None:
    overview = (await repo_overview(ctx)).splitlines()
    assert overview[0] == f"Repository: o/items, at commit {'b' * 12}."
    assert overview[1].startswith("5 files. Languages: python (3)")
    assert overview[2] == "Top-level directories: backend/ (4 files)."
    assert overview[3].startswith("The code graph has ")
    without = await repo_overview(dataclasses.replace(ctx, graph=None))
    assert without.splitlines()[-1].startswith("The code graph isn't built yet")


# ---------------------------------------------------------------- what fits (an audit's cases)


def _plain(root: Path, files: dict[str, str]) -> ToolContext:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    return _context(root, tree=sorted(files))


def _cursor_offset(note: str) -> int:
    cursor = note.split("next_cursor: ")[1].split(" ", maxsplit=1)[0]
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    return int(json.loads(raw)["o"])


async def test_a_page_too_big_to_show_whole_goes_on_from_what_it_showed(tmp_path: Path) -> None:
    """60 matching minified lines: what fits is shown, said so, and the next page starts at the
    first match that wasn't; a long listing likewise."""
    noise = [hashlib.sha256(str(n).encode()).hexdigest() * 6 for n in range(60)]
    long = "".join(f"needle {line}\n" for line in noise)
    names = {f"gen/{hashlib.sha256(str(n).encode()).hexdigest()}.ts": "x\n" for n in range(450)}
    ctx = _plain(tmp_path, {"big.min.js": long, **names})
    found = await run_tool(ctx, "grep", {"pattern": "needle", "limit": 50})
    shown = int(found.summary.split()[0])
    assert 0 < shown < 50
    assert _tokens(found) <= MAX_RESULT_TOKENS
    notes = [b["text"] for b in found.content if b["type"] == "text"]
    note = next(text for text in notes if "next_cursor" in text)
    assert note.startswith(f"Showing 1-{shown} of")
    assert _cursor_offset(note) == shown
    listed = await run_tool(ctx, "list_dir", {"path": "gen"})
    entries = listed.content[0]["text"].split("\n")[1:]
    assert 0 < len(entries) < 450
    page = listed.content[1]["text"]
    assert page.startswith(f"Showing 1-{len(entries)} of 450.")
    assert _cursor_offset(page) == len(entries)


async def test_lines_are_counted_as_grep_and_the_index_count_them(tmp_path: Path) -> None:
    """A form feed or a line separator in the code is no line break."""
    separated = f"s = 'a{chr(0x2028)}b'\nf()\n"
    ctx = _plain(tmp_path, {"a.py": "x = 1\n\f\ndef target():\n    pass\n", "b.js": separated})
    read = await run_tool(ctx, "read_code", {"path": "a.py"})
    assert read.content[0]["content"][0]["text"].split("\n")[2] == "3\tdef target():"
    js = await run_tool(ctx, "read_code", {"path": "b.js", "start_line": 2})
    assert js.content[0]["content"][0]["text"] == "2\tf()"


async def test_a_cursor_whose_offset_is_no_whole_number_is_refused(tmp_path: Path) -> None:
    ctx = _plain(tmp_path, {"a.py": "x\n"})
    args: dict[str, Any] = {"path": ""}
    key = args_key("list_dir", args)
    for offset in ("1e400", "2.5", "true"):
        raw = f'{{"k": "{key[:16]}", "o": {offset}}}'
        cursor = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        refused = await run_tool(ctx, "list_dir", {**args, "cursor": cursor})
        assert refused.is_error, offset
