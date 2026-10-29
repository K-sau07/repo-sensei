"""Parsing one file: grammars and query conventions, statuses, syntax errors, the timeout, and
the captures that the golden files (a Python and TypeScript project) don't cover."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from typing import cast

import pytest
from tree_sitter import Query

from sensei_engine.ingest.classify import MAX_PARSE_BYTES
from sensei_engine.parse.languages import (
    INHERITS,
    MAIN_CAPTURES,
    QueryKind,
    grammar,
    grammar_for,
    parser_for,
    query_dirs,
    query_source,
)
from sensei_engine.parse.parser import (
    ParseResult,
    ParseStatus,
    decorated,
    lines,
    parse_file,
)

GRAMMARS = ("python", "javascript", "typescript", "tsx", "java")


def _patterns(source: str) -> Iterator[str]:
    """The top-level patterns of a query (comments dropped, strings respected)."""
    depth, current = 0, ""
    for raw in source.splitlines():
        line = re.sub(r'"(?:\\.|[^"\\])*"', '""', raw).split(";")[0]
        if not line.strip() and depth == 0:
            continue
        current += raw + "\n"
        depth += line.count("(") + line.count("[") - line.count(")") - line.count("]")
        if depth == 0 and current.strip():
            yield current
            current = ""
    assert depth == 0


def _defs(result: ParseResult) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in result.captures[QueryKind.DEFS]:
        name = match.role("name")
        found.append((match.kind, result.text(name) if name is not None else ""))
    return found


def _captured(result: ParseResult, kind: QueryKind, role: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for match in result.captures[kind]:
        node = match.role(role)
        out.append((match.kind, result.text(node) if node is not None else ""))
    return out


# ---------------------------------------------------------------- grammars and queries


@pytest.mark.parametrize("name", GRAMMARS)
def test_every_query_compiles(name: str) -> None:
    found = grammar(name)
    assert set(found.queries) <= set(QueryKind)
    assert QueryKind.DEFS in found.queries
    # JSX element names exist only in the JSX dialects.
    assert (QueryKind.JSX in found.queries) == (name in ("javascript", "tsx"))


@pytest.mark.parametrize("name", GRAMMARS)
@pytest.mark.parametrize("kind", list(QueryKind))
def test_every_pattern_has_exactly_one_main_capture(name: str, kind: QueryKind) -> None:
    language = grammar(name).language
    roots = MAIN_CAPTURES[kind]
    for pattern in _patterns(query_source(name, kind)):
        query = Query(language, pattern)
        count = cast("int", query.capture_count)  # a property; the 0.25 stub says method
        names = [query.capture_name(i) for i in range(count)]
        main = [n for n in names if any(n == r or n.startswith(f"{r}.") for r in roots)]
        assert len(main) == 1, pattern


def test_inheritance_puts_a_grammars_own_patterns_first() -> None:
    assert query_dirs("tsx") == ["tsx", "jsx", "typescript", "ecma"]
    assert query_dirs("javascript") == ["javascript", "jsx", "ecma"]
    assert set(INHERITS) >= set(GRAMMARS)
    source = query_source("typescript", QueryKind.DEFS)
    assert source.index("typescript/defs.scm") < source.index("ecma/defs.scm")


def test_language_ids_map_to_grammars() -> None:
    assert grammar_for("jsx") is grammar("javascript")
    assert grammar_for("tsx") is grammar("tsx")
    assert grammar_for("java") is grammar("java")
    assert grammar_for("go") is None
    assert grammar_for(None) is None


def test_each_thread_gets_its_own_parser() -> None:
    python = grammar("python")
    mine = parser_for(python)
    assert parser_for(python) is mine
    theirs: list[object] = []
    thread = threading.Thread(target=lambda: theirs.append(parser_for(python)))
    thread.start()
    thread.join()
    assert theirs[0] is not mine


# ---------------------------------------------------------------- statuses and errors


def test_unsupported_binary_and_too_large_files_have_no_tree() -> None:
    assert parse_file("main.go", b"package main\n").status is ParseStatus.UNSUPPORTED
    binary = parse_file("blob.py", b"\x00\x01\x02" * 10)
    assert (binary.status, binary.tree) == (ParseStatus.BINARY, None)
    large = parse_file("big.py", b"x = 1\n" * (MAX_PARSE_BYTES // 6 + 1))
    assert (large.status, large.language) == (ParseStatus.TOO_LARGE, "python")


def test_the_language_comes_from_the_path_or_the_shebang() -> None:
    assert parse_file("tool", b"#!/usr/bin/env python3\nx = 1\n").language == "python"
    assert parse_file("a.mjs", b"export const a = 1\n").language == "javascript"
    assert parse_file("x.txt", b"x = 1", language="python").language == "python"


def test_syntax_errors_are_counted_and_located_not_fatal() -> None:
    result = parse_file("bad.py", b"def ok():\n    return 1\n\ndef f(:\n    pass\nclass\n")
    assert result.ok
    assert result.error_count == 2
    assert [(i.kind, i.line) for i in result.issues] == [("missing", 4), ("error", 6)]
    assert result.issues[0].expected == ")"
    # Definitions around the errors are still found.
    assert ("definition.function", "ok") in _defs(result)


def test_a_clean_file_has_no_errors() -> None:
    result = parse_file("ok.ts", "export function f(a: number): number { return a }\n")
    assert (result.status, result.error_count, result.issues) == (ParseStatus.PARSED, 0, ())


def test_a_parse_past_the_deadline_comes_back_without_a_tree() -> None:
    clock = iter([0.0, 5.0, 5.0])  # started, then read after parsing: 5 s later
    result = parse_file("slow.py", b"x = 1\n", timeout_seconds=2, clock=lambda: next(clock))
    assert (result.status, result.tree, result.captures) == (ParseStatus.TIMED_OUT, None, {})


def test_slow_queries_time_out_too() -> None:
    clock = iter([0.0, 0.0] + [10.0] * 10)  # the parse is quick, the first query isn't
    result = parse_file("q.py", b"x = 1\n", timeout_seconds=2, clock=lambda: next(clock))
    assert (result.status, result.tree) == (ParseStatus.TIMED_OUT, None)


def test_other_encodings_become_utf8_and_a_bom_is_dropped() -> None:
    bom = parse_file("a.py", b"\xef\xbb\xbfname = 1\n")
    assert bom.error_count == 0
    assert bom.source == b"name = 1\n"
    latin = parse_file("b.py", "café = 'é'\n".encode("latin-1"))
    assert latin.ok
    assert latin.source.decode("utf-8").startswith("caf")
    assert _defs(latin) == [("definition.variable", latin.source.decode().split(" ")[0])]
    utf16 = parse_file("c.py", "x = 1\n".encode("utf-16"))
    assert (utf16.source, utf16.error_count) == (b"x = 1\n", 0)


def test_lines_are_one_based_and_inclusive() -> None:
    result = parse_file("a.py", b"\n\ndef f():\n    pass\n")
    (match,) = result.captures[QueryKind.DEFS]
    assert lines(match.node) == (3, 4)
    assert result.tree is not None
    # The module starts at its first token and ends at the start of the line after the last
    # newline: it ends on line 4.
    assert lines(result.tree.root_node) == (3, 4)


# ---------------------------------------------------------------- python captures


def test_python_methods_nested_functions_and_class_fields() -> None:
    source = b"""
class Repo(Base):
    name: str = "x"
    @property
    def path(self):
        def inner():
            pass
        return inner
handler = lambda event: event
LIMIT = 10
type Ids = list[int]
__all__ = ["Repo", "LIMIT"]
"""
    assert _defs(parse_file("m.py", source)) == [
        ("definition.class", "Repo"),
        ("definition.field", "name"),
        ("definition.method", "path"),
        ("definition.function", "inner"),
        ("definition.function", "handler"),
        ("definition.variable", "LIMIT"),
        ("definition.type", "Ids"),
        ("definition.variable", "__all__"),
        ("export.all", '"Repo"'),
        ("export.all", '"LIMIT"'),
    ]


def test_python_imports() -> None:
    source = b"""from __future__ import annotations
import os.path as osp, sys
from .models import Item as Thing, User
from .. import deps
from x import *
"""
    result = parse_file("a.py", source, kinds=[QueryKind.IMPORTS])
    got = [
        (m.kind, *(result.text(n) for role in sorted(m.roles) for n in m.roles[role]))
        for m in result.captures[QueryKind.IMPORTS]
    ]
    assert got == [
        ("import.future", "annotations"),
        ("import", "osp", "os.path"),
        ("import", "sys"),
        ("import.from", "Thing", "Item", ".models"),  # roles in name order: alias, name, source
        ("import.from", "User", ".models"),
        ("import.from", "deps", ".."),
        ("import.wildcard", "*", "x"),
    ]


def test_python_decorators_and_what_they_decorate() -> None:
    source = b"""@app.get("/items/{id}")
@cache
async def read(id: int): ...

class A:
    @staticmethod
    def m(): ...
"""
    result = parse_file("a.py", source, kinds=[QueryKind.DECORATORS])
    found: list[tuple[str, bool, str | None]] = []
    for match in result.captures[QueryKind.DECORATORS]:
        target = decorated(match.node)
        name = target.child_by_field_name("name") if target is not None else None
        callee = result.text(match.roles["callee"][0])
        found.append((callee, "arguments" in match.roles, result.text(name) if name else None))
    assert found == [
        ("app.get", True, "read"),
        ("cache", False, "read"),
        ("staticmethod", False, "m"),
    ]


def test_python_strings_with_positions() -> None:
    result = parse_file("a.py", b'url = f"/api/{v}" "/items"\n', kinds=[QueryKind.STRINGS])
    assert [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.STRINGS]] == [
        ("string.concatenated", 'f"/api/{v}" "/items"'),
        ("string", 'f"/api/{v}"'),
        ("string", '"/items"'),
    ]


def test_python_references_tell_attributes_and_keywords_apart() -> None:
    result = parse_file("a.py", b"user.save(force=True)\n", kinds=[QueryKind.REFS])
    assert [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.REFS]] == [
        ("reference.identifier", "user"),
        ("reference.attribute", "save"),
        ("reference.keyword", "force"),
    ]


# ---------------------------------------------------------------- javascript and typescript


def test_javascript_components_hooks_and_exports() -> None:
    source = b"""import React, { memo } from "react"
class Store { items = []; static #secret; load() {} }
function useItems() { return [] }
const useCount = () => 0
export function ItemList() { return <ul /> }
const Row = memo(function Row() { return <li /> })
const Button = React.forwardRef((props, ref) => <button ref={ref} />)
const API_URL = "/api"
function* ids() {}
export default () => <main />
"""
    result = parse_file("app.jsx", source)
    assert result.language == "javascript"
    assert _defs(result) == [
        ("definition.class", "Store"),
        ("definition.field", "items"),
        ("definition.field", "#secret"),
        ("definition.method", "load"),
        ("definition.hook", "useItems"),
        ("definition.hook", "useCount"),
        ("export", ""),
        ("definition.component", "ItemList"),
        ("definition.component", "Row"),
        ("definition.component", "Button"),
        ("definition.variable", "API_URL"),
        ("definition.function", "ids"),
        ("export.default", ""),
        ("definition.function", ""),
    ]


def test_capitalized_functions_are_components_only_where_jsx_can_be() -> None:
    source = b"export function Config() { return 1 }\n"
    assert ("definition.function", "Config") in _defs(parse_file("a.ts", source))
    assert ("definition.component", "Config") in _defs(parse_file("a.tsx", source))


def test_typescript_declarations() -> None:
    source = b"""export interface Props { title: string; onSave(): void }
type Id = string
enum Color { Red }
abstract class Base { abstract run(): void }
namespace Api { export const v = 1 }
declare const VERSION: string
export function parse(a: string): number
export function parse(a: any) { return 0 }
class Service { private readonly url: string = "" }
"""
    defs = _defs(parse_file("types.ts", source))
    for expected in [
        ("definition.interface", "Props"),
        ("definition.field", "title"),
        ("definition.method", "onSave"),
        ("definition.type", "Id"),
        ("definition.enum", "Color"),
        ("definition.class", "Base"),
        ("definition.method", "run"),
        ("definition.module", "Api"),
        ("definition.variable", "VERSION"),
        ("definition.field", "url"),
    ]:
        assert expected in defs, expected
    # The overload signature and the implementation are both definitions.
    assert defs.count(("definition.function", "parse")) == 2


def test_ecmascript_imports_and_reexports() -> None:
    source = b"""import def, { a as b, c } from "./m"
import * as ns from "lib"
import "./side-effect.css"
import fs = require("fs")
export { x as y } from "./x"
export * from "./all"
export * as utils from "./utils"
const lazy = import("./lazy")
const old = require("old")
"""
    result = parse_file("i.ts", source, kinds=[QueryKind.IMPORTS])
    got = _captured(result, QueryKind.IMPORTS, "source")
    assert got == [
        ("import.default", '"./m"'),
        ("import.named", '"./m"'),
        ("import.named", '"./m"'),
        ("import.module", '"./m"'),
        ("import.namespace", '"lib"'),
        ("import.module", '"lib"'),
        ("import.module", '"./side-effect.css"'),
        ("import.require", '"fs"'),
        ("import.module", '"fs"'),
        ("import.reexport", '"./x"'),
        ("import.module", '"./x"'),
        ("import.reexport", '"./all"'),
        ("import.reexport", '"./utils"'),
        ("import.module", '"./utils"'),
        ("import.dynamic", '"./lazy"'),
        ("import.require", '"old"'),
    ]


def test_ecmascript_calls_and_tagged_templates() -> None:
    source = b"api.get(`/items/${id}`).then(done)\nnew Client(url)\nsql`select 1`\n"
    result = parse_file("c.js", source, kinds=[QueryKind.CALLS, QueryKind.STRINGS])
    assert _captured(result, QueryKind.CALLS, "name") == [
        ("call.method", "then"),
        ("call.method", "get"),
        ("call.new", "Client"),
        ("call", "sql"),
    ]
    assert [m.kind for m in result.captures[QueryKind.STRINGS]] == ["string.template"] * 2


def test_jsx_element_names() -> None:
    source = b"const A = () => <Layout.Main><Item id='1' /><></></Layout.Main>\n"
    result = parse_file("a.tsx", source, kinds=[QueryKind.JSX])
    assert _captured(result, QueryKind.JSX, "name") == [
        ("jsx.element", "Layout.Main"),
        ("jsx.element", "Item"),
    ]


def test_typescript_decorators_and_what_they_decorate() -> None:
    source = b"""@Controller("items")
export class ItemsController {
  @Inject() private svc: Service
  @Get(":id")
  @Auth
  find(id: string) {}
}
"""
    result = parse_file("c.ts", source, kinds=[QueryKind.DECORATORS])
    found: list[tuple[str, str | None]] = []
    for match in result.captures[QueryKind.DECORATORS]:
        target = decorated(match.node)
        name = target.child_by_field_name("name") if target is not None else None
        found.append((result.text(match.roles["callee"][0]), result.text(name) if name else None))
    assert found == [
        ("Controller", "ItemsController"),
        ("Inject", "svc"),
        ("Get", "find"),
        ("Auth", "find"),
    ]


def test_typescript_references_include_types() -> None:
    result = parse_file("r.ts", b"let u: User = repo.find({ id })\n", kinds=[QueryKind.REFS])
    assert [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.REFS]] == [
        ("reference.identifier", "u"),
        ("reference.type", "User"),
        ("reference.identifier", "repo"),
        ("reference.property", "find"),
        ("reference.identifier", "id"),
    ]
