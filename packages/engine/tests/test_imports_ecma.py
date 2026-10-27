"""JavaScript and TypeScript import resolution (3.3): statements, project configuration, module
resolution, barrels and graph facts; and the fastapi-template fixture against the TypeScript
compiler's answers (``fixtures/imports/fastapi-template.tsv``, see ``build_import_labels.py``)."""

from __future__ import annotations

import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.project import Project, is_config
from sensei_engine.graph.writer import CODE as CODE_LAYER
from sensei_engine.imports.ecma.config import ProjectConfig, parse_jsonc
from sensei_engine.imports.ecma.graph import extract, resolve
from sensei_engine.imports.ecma.resolve import (
    ModuleResolver,
    Resolution,
    ResolutionKind,
    Via,
    package_name,
)
from sensei_engine.imports.ecma.statements import (
    ImportKind,
    ImportStatement,
    LocalExport,
    extract_exports,
    extract_imports,
)
from sensei_engine.imports.ecma.table import (
    MAX_HOPS,
    FileExports,
    ImportTables,
    ImportTarget,
    TargetKind,
)
from sensei_engine.ingest.languages import detect_language
from sensei_engine.parse.parser import parse_file

FIXTURES = Path(__file__).parent / "fixtures"
ARCHIVE = FIXTURES / "fastapi-template.tar.gz"
LABELS = FIXTURES / "imports" / "fastapi-template.tsv"
CODE = frozenset({"javascript", "jsx", "typescript", "tsx"})


@dataclass
class Repo:
    files: dict[str, str | bytes]
    paths: list[str] = field(default_factory=list[str])
    contents: dict[str, bytes] = field(default_factory=dict[str, bytes])

    def __post_init__(self) -> None:
        self.paths = sorted(self.files)
        self.contents = {
            path: text.encode() if isinstance(text, str) else text
            for path, text in self.files.items()
        }

    def facts(self) -> dict[str, FileExports]:
        found: dict[str, FileExports] = {}
        for path, data in self.contents.items():
            if detect_language(path) in CODE:
                facts = extract(parse_file(path, data, language=detect_language(path)))
                if facts is not None:
                    found[path] = facts
        return found

    def project(self) -> Project:
        return Project(
            paths=frozenset(self.paths),
            configs={
                path: data.decode()
                for path, data in self.contents.items()
                if is_config(path) or path.endswith("pnpm-workspace.yaml")
            },
        )

    def resolver(self) -> ModuleResolver:
        return ModuleResolver(self.paths, ProjectConfig.load(self.paths, self.contents.get))

    def tables(self) -> ImportTables:
        return ImportTables(self.resolver(), self.facts())


def _statements(source: str, path: str = "web/src/x.tsx") -> list[ImportStatement]:
    return extract_imports(parse_file(path, source, language=detect_language(path)))


# ------------------------------------------------------------------ statements


def test_every_import_form_is_one_statement_per_binding() -> None:
    found = _statements(
        'import React, { useState as uS, type FC } from "react";\n'
        'import type { Item } from "@/client";\n'
        'import * as api from "./api";\n'
        'import "./styles.css";\n'
        'export { a as b } from "./barrel";\n'
        'export * from "./all";\n'
        'export * as ns from "./ns";\n'
        'const x = require("./cjs");\n'
        'const { p, q: r } = require("./cjs2");\n'
        'const Lazy = lazy(() => import("./pages/Lazy"));\n'
        "const t = import(`./tpl/${name}`);\n"
        "const u = import(`./literal`);\n"
    )
    assert [(s.kind, s.source, s.line, s.imported, s.local, s.type_only) for s in found] == [
        (ImportKind.DEFAULT, "react", 1, "default", "React", False),
        (ImportKind.NAMED, "react", 1, "useState", "uS", False),
        (ImportKind.NAMED, "react", 1, "FC", "FC", True),
        (ImportKind.NAMED, "@/client", 2, "Item", "Item", True),
        (ImportKind.NAMESPACE, "./api", 3, "*", "api", False),
        (ImportKind.MODULE, "./styles.css", 4, None, None, False),
        (ImportKind.REEXPORT, "./barrel", 5, "a", "b", False),
        (ImportKind.REEXPORT_ALL, "./all", 6, "*", None, False),
        (ImportKind.REEXPORT, "./ns", 7, "*", "ns", False),
        (ImportKind.REQUIRE, "./cjs", 8, "*", "x", False),
        (ImportKind.REQUIRE, "./cjs2", 9, "p", "p", False),
        (ImportKind.REQUIRE, "./cjs2", 9, "q", "r", False),
        (ImportKind.DYNAMIC, "./pages/Lazy", 10, None, None, False),
        (ImportKind.DYNAMIC, "./literal", 12, None, None, False),  # no substitution: a literal
    ]


def test_typescript_import_require_binds_the_module() -> None:
    (found,) = _statements('import fs = require("fs");\n', "web/x.ts")
    assert (found.kind, found.imported, found.local) == (ImportKind.REQUIRE, "*", "fs")


def test_exports_without_a_source_are_kept_wrapped_defaults_too() -> None:
    result = parse_file(
        "web/x.tsx",
        "export { local1, imported as renamed };\nexport default memo(forwardRef(Page));\n",
        language="tsx",
    )
    assert extract_exports(result) == [
        LocalExport("local1", "local1", 1),
        LocalExport("imported", "renamed", 1),
        LocalExport("Page", "default", 2),
    ]


# ------------------------------------------------------------------ configuration


def test_jsonc_comments_and_trailing_commas_outside_strings() -> None:
    text = '{\n // c\n "a": "x, } // not a comment", /* b */\n "b": [1, 2, /* z */ ],\n}'
    assert parse_jsonc(text) == {"a": "x, } // not a comment", "b": [1, 2]}


def test_extends_paths_and_base_url_resolve_against_the_config_that_sets_them() -> None:
    repo = Repo(
        {
            "tsconfig.base.json": '{"compilerOptions": {"paths": {"@lib/*": ["libs/*/src"]}}}',
            "apps/web/tsconfig.json": (
                '{"extends": ["../../tsconfig.base", "@tsconfig/strictest"],'
                ' "compilerOptions": {"baseUrl": "src"}}'
            ),
            "apps/web/src/main.ts": "",
            "apps/web/src/components/x.ts": "",
            "libs/ui/src/index.ts": "",
        }
    )
    config = ProjectConfig.load(repo.paths, repo.contents.get).config_for("apps/web/src/main.ts")
    assert config is not None
    assert config.base_url == "apps/web/src"
    # baseUrl set: inherited paths resolve against it (TypeScript's rule), not against the base.
    assert [(m.pattern, m.targets) for m in config.paths] == [
        ("@lib/*", ("apps/web/src/libs/*/src",))
    ]
    resolver = repo.resolver()
    assert resolver.resolve("apps/web/src/main.ts", "components/x").path == (
        "apps/web/src/components/x.ts"
    )


def test_paths_without_base_url_resolve_against_their_config_and_the_longest_prefix_wins() -> None:
    repo = Repo(
        {
            "web/tsconfig.json": (
                '{"compilerOptions": {"paths": {"@/*": ["./src/*"],'
                ' "@/queries/*": ["./src/api/queries/*"], "config": ["./src/config.ts"]}}}'
            ),
            "web/src/main.ts": "",
            "web/src/queries/a.ts": "",
            "web/src/api/queries/a.ts": "",
            "web/src/config.ts": "",
        }
    )
    resolver = repo.resolver()
    assert resolver.resolve("web/src/main.ts", "@/queries/a").path == "web/src/api/queries/a.ts"
    assert resolver.resolve("web/src/main.ts", "config").path == "web/src/config.ts"


def test_a_solution_style_config_hands_each_file_to_the_reference_that_includes_it() -> None:
    repo = Repo(
        {
            "web/tsconfig.json": (
                '{"files": [], "references": [{"path": "./tsconfig.app.json"},'
                ' {"path": "./tsconfig.node.json"}]}'
            ),
            "web/tsconfig.app.json": (
                '{"include": ["src"], "compilerOptions": {"paths": {"~/*": ["./src/*"]}}}'
            ),
            "web/tsconfig.node.json": '{"include": ["vite.config.ts"]}',
            "web/src/main.ts": "",
            "web/src/util.ts": "",
            "web/vite.config.ts": "",
        }
    )
    project = ProjectConfig.load(repo.paths, repo.contents.get)
    app = project.config_for("web/src/main.ts")
    node = project.config_for("web/vite.config.ts")
    assert app is not None
    assert app.path == "web/tsconfig.app.json"
    assert node is not None
    assert node.path == "web/tsconfig.node.json"
    assert repo.resolver().resolve("web/src/main.ts", "~/util").path == "web/src/util.ts"


def test_a_config_that_does_not_parse_is_skipped() -> None:
    repo = Repo({"web/tsconfig.json": "{ not json", "web/a.ts": "", "web/b.ts": ""})
    assert repo.resolver().resolve("web/a.ts", "./b").path == "web/b.ts"


# ------------------------------------------------------------------ resolution


@pytest.mark.parametrize(
    ("specifier", "path"),
    [
        ("./b", "web/src/b.ts"),
        ("./b.js", "web/src/b.ts"),  # TypeScript's ESM style names the .ts file
        ("./c", "web/src/c.tsx"),
        ("./dir", "web/src/dir/index.ts"),
        ("../d.json", "web/d.json"),
        ("./logo.svg?react", "web/src/logo.svg"),  # Vite's query suffix
        ("./e.mjs", "web/src/e.mjs"),
        ("/src/b", "web/src/b.ts"),  # root-relative: the package directory
        ("/icon.svg", "web/public/icon.svg"),  # then its public directory
    ],
)
def test_paths_name_a_file_with_or_without_extension(specifier: str, path: str) -> None:
    repo = Repo(
        {
            "web/package.json": '{"name": "web"}',
            "web/src/a.ts": "",
            "web/src/b.ts": "",
            "web/src/c.tsx": "",
            "web/src/dir/index.ts": "",
            "web/d.json": "{}",
            "web/src/logo.svg": "<svg/>",
            "web/src/e.mjs": "",
            "web/public/icon.svg": "<svg/>",
        }
    )
    assert repo.resolver().resolve("web/src/a.ts", specifier) == Resolution(
        ResolutionKind.FILE,
        path=path,
        via=Via.ROOT if specifier.startswith("/") else Via.RELATIVE,
    )


def test_a_relative_import_of_nothing_or_out_of_the_repository_is_unresolved() -> None:
    resolver = Repo({"a.ts": ""}).resolver()
    assert resolver.resolve("a.ts", "./gone").kind is ResolutionKind.UNRESOLVED
    assert resolver.resolve("a.ts", "../../etc/passwd").kind is ResolutionKind.UNRESOLVED


def test_builtins_packages_and_their_names() -> None:
    repo = Repo({"package.json": '{"dependencies": {"util": "^0.12"}}', "a.ts": ""})
    resolver = repo.resolver()
    assert resolver.resolve("a.ts", "node:fs").package == "node:fs"
    assert resolver.resolve("a.ts", "path").package == "node:path"
    assert resolver.resolve("a.ts", "util").package == "util"  # the npm package it declares
    assert resolver.resolve("a.ts", "@tanstack/react-query/devtools") == Resolution(
        ResolutionKind.EXTERNAL, package="@tanstack/react-query"
    )
    assert package_name("@scope/name/sub/x") == ("@scope/name", "./sub/x")
    assert package_name("lodash") == ("lodash", ".")


def test_a_declared_dependency_beats_a_base_url_declaration_file() -> None:
    repo = Repo(
        {
            "web/package.json": '{"dependencies": {"i18next": "^25"}}',
            "web/tsconfig.json": '{"compilerOptions": {"baseUrl": "src"}}',
            "web/src/i18next.d.ts": 'declare module "i18next" {}',
            "web/src/utils.ts": "",
            "web/src/main.ts": "",
        }
    )
    resolver = repo.resolver()
    assert resolver.resolve("web/src/main.ts", "i18next").kind is ResolutionKind.EXTERNAL
    assert resolver.resolve("web/src/main.ts", "utils").via is Via.BASE_URL


def test_a_broken_alias_is_unresolved_but_a_catch_all_pattern_falls_through() -> None:
    repo = Repo(
        {
            "web/tsconfig.json": (
                '{"compilerOptions": {"paths": {"@/*": ["./src/*"], "*": ["./types/*"]}}}'
            ),
            "web/src/main.ts": "",
        }
    )
    resolver = repo.resolver()
    assert resolver.resolve("web/src/main.ts", "@/gone") == Resolution(
        ResolutionKind.UNRESOLVED, via=Via.PATHS
    )
    assert resolver.resolve("web/src/main.ts", "react").kind is ResolutionKind.EXTERNAL


def test_workspace_packages_resolve_through_exports_to_their_sources() -> None:
    repo = Repo(
        {
            "package.json": '{"workspaces": ["packages/*"]}',
            "packages/ui/package.json": (
                '{"name": "@acme/ui", "exports": {".": {"types": "./dist/index.d.ts",'
                ' "import": "./dist/index.mjs"}, "./button": "./src/button.tsx",'
                ' "./icons/*": "./src/icons/*.tsx"}}'
            ),
            "packages/ui/src/index.ts": "",
            "packages/ui/src/button.tsx": "",
            "packages/ui/src/icons/star.tsx": "",
            "packages/legacy/package.json": '{"name": "legacy", "main": "lib/main.js"}',
            "packages/legacy/src/main.ts": "",
            "apps/web/package.json": '{"name": "web", "dependencies": {"@acme/ui": "*"}}',
            "apps/web/src/a.ts": "",
            "tools/package.json": '{"name": "tools"}',
        }
    )
    resolver = repo.resolver()
    found = resolver.resolve("apps/web/src/a.ts", "@acme/ui")
    assert (found.path, found.package, found.via) == (
        "packages/ui/src/index.ts",  # dist/index.mjs isn't built: its source
        "@acme/ui",
        Via.PACKAGE,
    )
    assert resolver.resolve("apps/web/src/a.ts", "@acme/ui/button").path == (
        "packages/ui/src/button.tsx"
    )
    assert resolver.resolve("apps/web/src/a.ts", "@acme/ui/icons/star").path == (
        "packages/ui/src/icons/star.tsx"
    )
    assert resolver.resolve("apps/web/src/a.ts", "legacy").path == "packages/legacy/src/main.ts"
    # Neither a workspace nor declared: a package of that name from npm.
    assert resolver.resolve("apps/web/src/a.ts", "tools").kind is ResolutionKind.EXTERNAL


def test_pnpm_workspaces_count_as_workspaces() -> None:
    repo = Repo(
        {
            "pnpm-workspace.yaml": "packages:\n  - 'libs/*'\n  - '!libs/skip'\n",
            "libs/core/package.json": '{"name": "core", "main": "index.js"}',
            "libs/core/index.js": "",
            "app/a.ts": "",
        }
    )
    assert repo.resolver().resolve("app/a.ts", "core").path == "libs/core/index.js"


def test_docusaurus_site_and_swizzled_theme_aliases() -> None:
    repo = Repo(
        {
            "docs/package.json": '{"dependencies": {"@docusaurus/core": "3"}}',
            "docs/src/components/Card.tsx": "",
            "docs/src/theme/Footer/index.tsx": "",
            "docs/src/pages/index.tsx": "",
        }
    )
    resolver = repo.resolver()
    importer = "docs/src/pages/index.tsx"
    assert resolver.resolve(importer, "@site/src/components/Card").path == (
        "docs/src/components/Card.tsx"
    )
    assert resolver.resolve(importer, "@theme/Footer").path == "docs/src/theme/Footer/index.tsx"
    assert resolver.resolve(importer, "@theme/Layout").kind is ResolutionKind.EXTERNAL
    # The swizzled file itself imports the original's types.
    swizzled = "docs/src/theme/Footer/index.tsx"
    assert resolver.resolve(swizzled, "@theme/Footer").kind is ResolutionKind.EXTERNAL


# ------------------------------------------------------------------ barrels


def _symbol(target: ImportTarget) -> tuple[TargetKind, str | None, int]:
    return target.kind, target.symbol_key, target.hops


def test_barrel_chains_reach_the_definition() -> None:
    repo = Repo(
        {
            "web/src/ui/button.tsx": "export function Button() { return <b/> }\n",
            "web/src/ui/card.tsx": "const Card = () => <div/>\nexport default memo(Card)\n",
            "web/src/ui/input.ts": "export const Input = 1\n",
            "web/src/ui/inner.ts": 'export * from "./input"\n',
            "web/src/ui/index.ts": (
                'export { Button as Btn } from "./button"\n'
                'export { default as Card } from "./card"\n'
                'export * from "./inner"\n'
                'import { Button } from "./button"\n'
                "export { Button }\n"
                'export * as all from "./input"\n'
            ),
            "web/src/page.tsx": (
                'import { Btn, Card, Input, Button, all } from "./ui"\n'
                'import CardDefault from "./ui/card"\n'
            ),
        }
    )
    table = repo.tables().table("web/src/page.tsx")
    assert _symbol(table["Btn"].target) == (
        TargetKind.SYMBOL,
        "tsx web/src/ui/button.tsx/Button().",
        1,
    )
    assert _symbol(table["Card"].target) == (
        TargetKind.SYMBOL,
        "tsx web/src/ui/card.tsx/Card().",
        1,
    )
    assert _symbol(table["Input"].target) == (
        TargetKind.SYMBOL,
        "ts web/src/ui/input.ts/Input.",
        2,
    )
    assert _symbol(table["Button"].target)[1] == "tsx web/src/ui/button.tsx/Button()."
    assert _symbol(table["all"].target) == (TargetKind.MODULE, "ts web/src/ui/input.ts/", 1)
    assert _symbol(table["CardDefault"].target)[1] == "tsx web/src/ui/card.tsx/Card()."


def test_a_chain_longer_than_the_limit_or_a_cycle_ends_at_the_module() -> None:
    files: dict[str, str | bytes] = {
        f"web/b{i}.ts": f'export * from "./b{i + 1}"\n' for i in range(MAX_HOPS + 2)
    }
    files[f"web/b{MAX_HOPS + 2}.ts"] = "export const deep = 1\n"
    files["web/c1.ts"] = 'export * from "./c2"\n'
    files["web/c2.ts"] = 'export * from "./c1"\n'
    files["web/main.ts"] = 'import { deep } from "./b0"\nimport { loop } from "./c1"\n'
    table = Repo(files).tables().table("web/main.ts")
    assert _symbol(table["deep"].target) == (TargetKind.MODULE, "ts web/b0.ts/", 0)
    assert _symbol(table["loop"].target) == (TargetKind.MODULE, "ts web/c1.ts/", 0)


def test_a_javascript_module_s_types_come_from_its_declaration_file() -> None:
    repo = Repo(
        {
            "t/policy.mjs": "export const KEY = 1\n",
            "t/policy.d.mts": "export type Node = { id: string }\nexport const KEY: number\n",
            "t/a.ts": 'import { KEY, type Node } from "./policy.mjs"\n',
        }
    )
    table = repo.tables().table("t/a.ts")
    assert table["KEY"].target.symbol_key == "js t/policy.mjs/KEY."
    assert table["Node"].target.symbol_key == "ts t/policy.d.mts/Node#"


def test_a_barrel_re_exporting_a_package_keeps_the_package_and_name() -> None:
    repo = Repo(
        {
            "t/fixtures.ts": 'import { expect } from "@playwright/test"\nexport { expect }\n',
            "t/a.spec.ts": 'import { expect } from "./fixtures"\n',
        }
    )
    target = repo.tables().table("t/a.spec.ts")["expect"].target
    assert (target.kind, target.package, target.name, target.hops) == (
        TargetKind.EXTERNAL,
        "@playwright/test",
        "expect",
        1,
    )


# ------------------------------------------------------------------ graph facts


def test_resolve_gives_import_edges_and_shared_external_nodes() -> None:
    repo = Repo(
        {
            "web/package.json": '{"dependencies": {"react": "19"}}',
            "web/src/util.ts": "export function cn() {}\n",
            "web/src/a.tsx": (
                'import { useState } from "react"\n'
                'import * as React from "react"\n'
                'import { cn } from "./util"\n'
                'import type { Missing } from "./util"\n'
                'import "./a.css"\n'
                'import "./gone"\n'
            ),
            "web/src/b.tsx": 'import { useState } from "react"\n',
            "web/src/a.css": "",
        }
    )
    nodes, edges, stats = resolve(repo.project(), repo.facts())
    assert sorted((n.key, n.kind, n.name, n.owner) for n in nodes) == [
        ("external_symbol npm:react/useState", NodeKind.EXTERNAL_SYMBOL, "useState", None),
        ("package npm:react", NodeKind.PACKAGE, "react", None),
    ]
    found = sorted(
        (e.src, e.dst, e.evidence_line, dict(e.attrs), e.owner)
        for e in edges
        if e.owner == "web/src/a.tsx"
    )
    assert found == [
        ("tsx web/src/a.tsx/", "external_symbol npm:react/useState", 1, {}, "web/src/a.tsx"),
        ("tsx web/src/a.tsx/", "package npm:react", 2, {}, "web/src/a.tsx"),
        ("tsx web/src/a.tsx/", "ts web/src/util.ts/", 4, {"type_only": True}, "web/src/a.tsx"),
        ("tsx web/src/a.tsx/", "ts web/src/util.ts/cn().", 3, {}, "web/src/a.tsx"),
    ]  # the stylesheet has no node; ./gone resolves to nothing
    assert {(e.type, e.provenance, e.confidence) for e in edges} == {
        (EdgeType.IMPORTS, Provenance.IMPORT_RESOLVED, None)  # the provenance's: 0.9
    }
    CODE_LAYER.check(nodes, edges)  # the graph stage's layer: it raises on anything else
    assert (stats.internal, stats.resolved, stats.external) == (4, 3, 3)
    assert stats.unresolved == [("web/src/a.tsx", 6, "./gone")]
    assert stats.tables["tsx web/src/a.tsx/"] == {
        "useState": "external_symbol npm:react/useState",
        "React": "package npm:react",
        "cn": "ts web/src/util.ts/cn().",
        "Missing": "ts web/src/util.ts/",
    }


def test_extract_skips_files_that_are_not_javascript_or_did_not_parse() -> None:
    assert extract(parse_file("app/x.py", "import os\n", language="python")) is None
    assert extract(parse_file("web/x.ts", b"\x00\x01binary", language="typescript")) is None


# ------------------------------------------------------------------ fastapi-template


def _key_name(key: str) -> str:
    """A top-level definition's name from its key: ``ts a.ts/cn().`` is ``cn``."""
    descriptor = re.sub(r"~\d+$", "", key.rsplit("/", 1)[-1])
    return re.sub(r"(\(\)\.|#|\.)$", "", descriptor).strip("`")


@pytest.fixture(scope="module")
def fastapi_template() -> Repo:
    files: dict[str, str | bytes] = {}
    with tarfile.open(ARCHIVE) as archive:
        for member in archive.getmembers():
            if member.isfile():
                handle = archive.extractfile(member)
                assert handle is not None
                files[member.name.removeprefix("fastapi-template/")] = handle.read()
    return Repo(files)


def test_fastapi_template_imports_match_the_typescript_compiler(fastapi_template: Repo) -> None:
    tables = fastapi_template.tables()
    ours: dict[tuple[str, int, str, str], ImportTarget] = {}
    for path in tables.files:
        for binding in tables.bindings(path):
            s = binding.statement
            ours[(path, s.line, s.source, s.local or "-")] = binding.target
    rows = [line.split("\t") for line in LABELS.read_text().splitlines()[1:]]
    agree: list[tuple[str, ...]] = []
    differ: list[tuple[str, ...]] = []
    for importer, line, source, local, kind, target, name in rows:
        found = ours.get((importer, int(line), source, local))
        if found is None:
            matches = False
        elif kind == "symbol":
            matches = (
                found.kind is TargetKind.SYMBOL
                and found.path == target
                and _key_name(found.symbol_key or "") == name
            )
        elif kind == "module":
            matches = found.path == target
        elif kind == "external":
            matches = found.kind is TargetKind.EXTERNAL and found.package == target
        else:
            matches = found.kind is TargetKind.FILE and found.path == target
        (agree if matches else differ).append((importer, line, source, local, kind, target))
    assert len(rows) == 1013
    rate = len(agree) / len(rows)
    assert rate >= 0.95, differ[:20]  # the spec's bar (3.3.5); measured: every one
    assert differ == []
