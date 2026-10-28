"""JavaScript and TypeScript calls, React structure and tests as graph edges (3.5): what the pool
extracts (:mod:`sensei_engine.imports.ecma.calls`) and how it is bound
(:mod:`sensei_engine.imports.ecma.code`); and the fastapi-template's against the TypeScript
compiler's answers (``fixtures/imports/fastapi-template-code.tsv``: ``build_code_labels.py``)."""

from __future__ import annotations

import re
import tarfile
from pathlib import Path

import pytest

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.writer import CODE, GraphEdge, GraphNode
from sensei_engine.imports.ecma.calls import CallbackFact, CallFact, JsxFact, TestFact
from sensei_engine.imports.ecma.graph import EcmaFacts, extract, resolve
from sensei_engine.ingest.languages import detect_language
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols

SOURCES = frozenset({"javascript", "jsx", "typescript", "tsx"})


def _facts(files: dict[str, str]) -> tuple[Project, dict[str, EcmaFacts]]:
    facts: dict[str, EcmaFacts] = {}
    definitions: dict[str, list[Definition]] = {}
    for path, text in files.items():
        language = detect_language(path)
        if language not in SOURCES:
            continue
        parsed = parse_file(path, text, language=language)
        found = extract(parsed)
        assert found is not None
        facts[path] = found
        definitions[path] = [
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in extract_symbols(parsed, language)
        ]
    project = Project(
        paths=frozenset(files),
        configs={p: t for p, t in files.items() if is_config(p)},
        definitions=definitions,
    )
    return project, facts


def _graph(files: dict[str, str]) -> tuple[list[GraphNode], list[GraphEdge]]:
    project, facts = _facts(files)
    nodes, edges, _ = resolve(project, facts)
    CODE.check(nodes, edges)
    return nodes, edges


def _edges(
    edges: list[GraphEdge], *types: EdgeType
) -> set[tuple[str, str, EdgeType, Provenance, float | None, int | None]]:
    return {
        (e.src, e.dst, e.type, e.provenance, e.confidence, e.evidence_line)
        for e in edges
        if e.type in types
    }


I = Provenance.IMPORT_RESOLVED  # noqa: E741 - short names for the expected tables
R = Provenance.RECEIVER_HEURISTIC


# ------------------------------------------------------------------ extraction


def test_calls_elements_callbacks_and_tests_are_extracted_with_their_definitions() -> None:
    _, facts = _facts(
        {
            "web/src/Page.tsx": (
                'import { api } from "./api";\n'
                "class Store extends Base {\n"
                "  load() { this.fetch(); super.init(); }\n"
                "  fetch() { return api.items.list(); }\n"
                "}\n"
                "export function Page({ onSave }) {\n"
                "  const handleClick = () => { helper(); };\n"
                "  return <Layout.Main><Button onClick={handleClick} n={1} />"
                "<div/><my-el/></Layout.Main>;\n"
                "}\n"
                "function helper() { new Store().load(); }\n"
                'it("renders", () => { render(<Page />); helper(); });\n'
            )
        }
    )
    code = facts["web/src/Page.tsx"].code
    key = "tsx web/src/Page.tsx/"
    assert code.calls == (
        CallFact(3, "fetch", "this", f"{key}Store#load()."),
        CallFact(3, "init", "super", f"{key}Store#load()."),
        CallFact(4, "list", "api.items", f"{key}Store#fetch()."),
        CallFact(7, "helper", None, f"{key}Page().handleClick()."),
        CallFact(10, "load", "", f"{key}helper()."),
        CallFact(10, "Store", None, f"{key}helper().", new=True),
        CallFact(11, "it", None, key),
        CallFact(11, "render", None, key, test=0),
        CallFact(11, "helper", None, key, test=0),
    )
    assert code.elements == (
        JsxFact(8, "Layout.Main", f"{key}Page()."),
        JsxFact(8, "Button", f"{key}Page()."),
        JsxFact(11, "Page", key, test=0),
    )  # <div> and <my-el> are the host's
    assert code.callbacks == (CallbackFact(8, "onClick", "handleClick", f"{key}Page()."),)
    assert code.tests == (TestFact(11, 11, "renders", key),)


def test_test_modifiers_count_and_a_call_without_a_function_is_not_a_test() -> None:
    _, facts = _facts(
        {
            "t/a.test.ts": (
                'test.only("one", () => {});\n'
                'it.skip("two", async () => {});\n'
                'it("no body");\n'
                "it(name, () => {});\n"
            )
        }
    )
    assert [t.title for t in facts["t/a.test.ts"].code.tests] == ["one", "two"]


# ------------------------------------------------------------------ calls


def test_scope_and_imports_bind_calls_at_import_resolved() -> None:
    _, edges = _graph(
        {
            "web/src/util.ts": (
                "export function cn() {}\nexport class Store { static create() {} }\n"
            ),
            "web/src/index.ts": 'export * from "./util";\n',
            "web/src/api.ts": "export function listItems() {}\n",
            "web/src/a.ts": (
                'import { cn, Store } from "./index";\n'
                'import * as api from "./api";\n'
                "function outer() {\n"
                "  function inner() {}\n"
                "  inner(); cn(); api.listItems(); Store.create(); new Store();\n"
                "  fetch(); missing.go();\n"
                "}\n"
            ),
        }
    )
    src = "ts web/src/a.ts/outer()."
    assert _edges(edges, EdgeType.CALLS) == {
        (src, "ts web/src/a.ts/outer().inner().", EdgeType.CALLS, I, None, 5),
        (src, "ts web/src/util.ts/cn().", EdgeType.CALLS, I, None, 5),
        (src, "ts web/src/api.ts/listItems().", EdgeType.CALLS, I, None, 5),
        (src, "ts web/src/util.ts/Store#create().", EdgeType.CALLS, I, None, 5),
        (src, "ts web/src/util.ts/Store#", EdgeType.CALLS, I, None, 5),
    }  # fetch is a global; missing.go has no method named go anywhere


def test_an_awaited_call_with_type_arguments_is_a_call() -> None:
    """``await f<T>(x)``: tree-sitter's TypeScript puts the ``await`` inside the call."""
    _, edges = _graph(
        {
            "web/src/api.ts": "export async function load<T>(x: T) { return x }\n",
            "web/src/a.ts": (
                'import * as api from "./api";\n'
                'import { load } from "./api";\n'
                "export async function run() {\n"
                '  await load<string>("x");\n'
                "  await api.load<number>(1);\n"
                "}\n"
            ),
        }
    )
    src, dst = "ts web/src/a.ts/run().", "ts web/src/api.ts/load()."
    assert _edges(edges, EdgeType.CALLS) == {
        (src, dst, EdgeType.CALLS, I, None, 4),
        (src, dst, EdgeType.CALLS, I, None, 5),
    }


def test_an_object_literal_s_methods_are_called_through_its_name() -> None:
    _, edges = _graph(
        {
            "web/api.ts": "export const api = { list() {}, get: () => 1 };\n",
            "web/a.ts": (
                'import { api } from "./api";\n'
                "const local = { save() {} };\n"
                "function go() { api.list(); local.save(); api.get(); }\n"
            ),
        }
    )
    assert {e.dst for e in edges if e.type is EdgeType.CALLS} == {
        "ts web/api.ts/api.list().",
        "ts web/a.ts/local.save().",
    }  # get is an arrow function in a property: not a definition


def test_this_binds_to_the_enclosing_class_at_the_receiver_heuristic() -> None:
    _, edges = _graph(
        {
            "web/a.ts": (
                "class Repo {\n  save() { this.validate(); this.nothing(); }\n  validate() {}\n}\n"
            )
        }
    )
    assert _edges(edges, EdgeType.CALLS) == {
        ("ts web/a.ts/Repo#save().", "ts web/a.ts/Repo#validate().", EdgeType.CALLS, R, None, 2)
    }


@pytest.mark.parametrize(
    ("methods", "expected"),
    [
        (1, {(Provenance.NAME_UNIQUE, None)}),
        (2, {(Provenance.NAME_AMBIGUOUS, None)}),  # 0.2/2 is the provenance's default for k=2
        (6, set[tuple[Provenance, float | None]]()),  # too many to guess
    ],
)
def test_a_method_of_an_unknown_receiver_is_guessed_by_name(
    methods: int, expected: set[tuple[Provenance, float | None]]
) -> None:
    files = {f"web/m{i}.ts": f"class C{i} {{ archiveRecord() {{}} }}\n" for i in range(methods)}
    files["web/a.ts"] = "function go(item) { item.archiveRecord(); item.map(); item.read(); }\n"
    files["web/mock.ts"] = "const reader = { read() {}, archiveRecord() {} };\n"  # not classes
    _, edges = _graph(files)
    found = [e for e in edges if e.src == "ts web/a.ts/go()."]
    assert {(e.provenance, e.confidence) for e in found} == expected
    assert len(found) == (methods if methods <= 5 else 0)  # each candidate, map never


def test_a_known_value_s_members_are_not_guessed() -> None:
    _, edges = _graph(
        {
            "web/m.ts": "class C { archiveRecord() {} }\n",
            "web/a.ts": (
                'import { store } from "./s";\n'
                "const local = 1;\n"
                "function go() { store.archiveRecord(); local.archiveRecord(); }\n"
            ),
            "web/s.ts": "export const store = {};\n",
        }
    )
    assert [e for e in edges if e.type is EdgeType.CALLS] == []


def test_packages_are_called_through_their_external_symbols() -> None:
    nodes, edges = _graph(
        {
            "web/package.json": '{"dependencies": {"axios": "1", "sonner": "2"}}',
            "web/a.ts": (
                'import axios from "axios";\n'
                'import { toast } from "sonner";\n'
                "function go() { axios.get(); toast(); }\n"
            ),
        }
    )
    assert {(e.dst, e.type) for e in edges if e.src == "ts web/a.ts/go()."} == {
        ("external_symbol npm:axios/get", EdgeType.CALLS),
        ("external_symbol npm:sonner/toast", EdgeType.CALLS),
    }
    assert {n.key: n.owner for n in nodes if n.key.endswith("/get")} == {
        "external_symbol npm:axios/get": None
    }


# ------------------------------------------------------------------ react


def test_hooks_components_and_callbacks() -> None:
    _, edges = _graph(
        {
            "web/package.json": '{"dependencies": {"react": "19", "@radix-ui/react-dialog": "1"}}',
            "web/src/hooks.ts": "export function useAuth() { return useState(); }\n",
            "web/src/ui/index.tsx": "export function Button() { return <b/> }\n",
            "web/src/Page.tsx": (
                'import { useState } from "react";\n'
                'import { useAuth } from "./hooks";\n'
                'import * as ui from "./ui";\n'
                'import { Button } from "./ui";\n'
                'import { Dialog } from "@radix-ui/react-dialog";\n'
                "function Row() { return <li/> }\n"
                "export function Page() {\n"
                "  const [x] = useState(0); useAuth();\n"
                "  function handleClick() {}\n"
                "  return <ui.Button><Button onClick={handleClick} />"
                "<Row /><Dialog /></ui.Button>;\n"
                "}\n"
            ),
        }
    )
    page = "tsx web/src/Page.tsx/Page()."
    assert {(e.dst, e.type) for e in edges if e.src == page} == {
        ("external_symbol npm:react/useState", EdgeType.USES_HOOK),
        ("ts web/src/hooks.ts/useAuth().", EdgeType.USES_HOOK),
        ("tsx web/src/ui/index.tsx/Button().", EdgeType.RENDERS),
        ("tsx web/src/Page.tsx/Row().", EdgeType.RENDERS),
        ("external_symbol npm:@radix-ui/react-dialog/Dialog", EdgeType.RENDERS),
        ("tsx web/src/Page.tsx/Page().handleClick().", EdgeType.REFERENCES),
    }


# ------------------------------------------------------------------ tests


def test_test_blocks_are_test_nodes_with_tests_edges_instead_of_module_calls() -> None:
    nodes, edges = _graph(
        {
            "web/package.json": '{"devDependencies": {"vitest": "3"}}',
            "web/src/sum.ts": "export function sum() {}\n",
            "web/src/total.tsx": "export function Total() { return <b/> }\n",
            "web/src/sum.test.tsx": (
                'import { expect, it } from "vitest";\n'
                'import { sum } from "./sum";\n'
                'import { Total } from "./total";\n'
                'it("adds", () => {\n'
                "  expect(sum()).toBe(3);\n"
                "  <Total />;\n"
                "});\n"
                "sum();\n"
            ),
        }
    )
    test_key = "test web/src/sum.test.tsx:4"
    (test,) = [n for n in nodes if n.kind is NodeKind.TEST]
    assert (test.key, test.name, test.start_line, test.end_line, test.owner) == (
        test_key,
        "adds",
        4,
        7,
        "web/src/sum.test.tsx",
    )
    module = "tsx web/src/sum.test.tsx/"
    assert _edges(edges, EdgeType.TESTS, EdgeType.CALLS, EdgeType.RENDERS) == {
        (test_key, "ts web/src/sum.ts/sum().", EdgeType.TESTS, I, 0.7, 5),
        (test_key, "tsx web/src/total.tsx/Total().", EdgeType.TESTS, I, 0.7, 6),
        (module, "ts web/src/sum.ts/sum().", EdgeType.CALLS, I, None, 8),
        # it() itself is vitest's: a CALLS edge from the module, at the top.
        (module, "external_symbol npm:vitest/it", EdgeType.CALLS, I, None, 4),
    }  # expect and toBe in the block are the framework's: no TESTS edge


# ------------------------------------------------------------------ fastapi-template

FIXTURES = Path(__file__).parent / "fixtures"
ARCHIVE = FIXTURES / "fastapi-template.tar.gz"
LABELS = FIXTURES / "imports" / "fastapi-template-code.tsv"
_KEY = re.compile(r"^\S+ (.+?\.(?:tsx|ts|jsx|js|mjs|cjs|mts|cts))/(.*)$")
_DESCRIPTOR = re.compile(r"(`(?:[^`]|``)*`|[\w$]+)(?:\(\)\.|#|\.)(?:~\d+)?")


def _declared(key: str) -> tuple[str, str] | None:
    """A definition key's file and name: ``tsx a.tsx/Page().onSave().`` is (a.tsx, onSave)."""
    match = _KEY.match(key)
    names = _DESCRIPTOR.findall(match.group(2)) if match else []
    return (match.group(1), names[-1].strip("`")) if match and names else None


def test_fastapi_template_frontend_edges_match_the_typescript_compiler() -> None:
    files: dict[str, str] = {}
    with tarfile.open(ARCHIVE) as archive:
        for member in archive.getmembers():
            handle = archive.extractfile(member) if member.isfile() else None
            if handle is None:
                continue
            path = member.name.removeprefix("fastapi-template/")
            code = detect_language(path) in SOURCES or is_config(path)
            files[path] = handle.read().decode("utf-8", "replace") if code else ""
    _, edges = _graph(files)
    labels: dict[tuple[str, int], set[tuple[str, str]]] = {}
    for row in LABELS.read_text().splitlines()[1:]:
        path, line, _name, target, declared = row.split("\t")
        labels.setdefault((path, int(line)), set()).add((target, declared))
    right: list[GraphEdge] = []
    wrong: list[GraphEdge] = []
    for edge in edges:
        if edge.type is EdgeType.IMPORTS or edge.dst.startswith(("external_symbol ", "package ")):
            continue  # imports are 3.3's; packages aren't the compiler's to tell (none installed)
        assert edge.evidence_path is not None
        assert edge.evidence_line is not None
        declared = _declared(edge.dst)
        found = labels.get((edge.evidence_path, edge.evidence_line), set())
        (right if declared in found else wrong).append(edge)
    judged = len(right) + len(wrong)
    assert judged >= 200  # the spec's sample (3.5.5); every internal edge is judged here
    assert len(right) / judged >= 0.9, wrong[:10]
    assert wrong == []  # measured: every one
