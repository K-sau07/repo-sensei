"""Renamed copies: one consistent mapping for names, paths and gold, with lines unchanged."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from sensei_engine.ingest.discover import Discovery, discover
from sensei_engine.ingest.languages import detect_language
from sensei_engine.parse.parser import parse_file
from sensei_engine.parse.stage import parseable, read_source
from sensei_engine.symbols.extract import SymbolKind, extract_symbols
from sensei_evals import paths
from sensei_evals.cli import app
from sensei_evals.golden import parse_python, read_lines, read_text, validate_suite
from sensei_evals.python_names import PYTHON_NAMES
from sensei_evals.rename import (
    JS,
    PROTECTED,
    PYTHON,
    PythonModules,
    RenamePlan,
    RenameResult,
    TranslateError,
    commit_tree,
    external_names,
    extracted_symbols,
    labeling_symbols,
    language_of,
    make_plan,
    plan_for,
    rename_tree,
    split_words,
    suite_yaml,
    translate_query,
    translate_suite,
)
from sensei_evals.repos import PinnedRepo, mapping_path
from sensei_evals.schema import GoldenItem, GoldTarget, Layer, QueryClass, Suite, parse_suite

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "packages" / "engine" / "tests" / "fixtures" / "fastapi-template.tar.gz"
SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e"
PY = (PYTHON,)


@pytest.mark.parametrize(
    ("identifier", "words"),
    [
        ("user_id", ["user", "id"]),
        ("ItemsTable", ["Items", "Table"]),
        ("getHTTPResponse2", ["get", "HTTP", "Response2"]),
        ("v2", ["v2"]),
        ("API_V1_STR", ["API", "V1", "STR"]),
        ("user-settings", ["user", "settings"]),
        ("_private", ["private"]),
    ],
)
def test_split_words(identifier: str, words: list[str]) -> None:
    assert split_words(identifier) == words


def test_plan_keeps_style_and_maps_words_consistently() -> None:
    names = ["Widgets", "widgets", "WidgetsTable", "widgets_table", "readWidgets", "WIDGETS_MAX"]
    plan = make_plan([*names, "_widgets"], set())
    item, table = plan.words["widgets"], plan.words["table"]
    cap = item.capitalize()
    assert plan.tokens == {
        "WIDGETS_MAX": f"{item.upper()}_{plan.words['max'].upper()}",
        "Widgets": cap,
        "WidgetsTable": cap + table.capitalize(),
        "_widgets": f"_{item}",
        "widgets": item,
        "widgets_table": f"{item}_{table}",
        "readWidgets": plan.words["read"] + cap,
    }


def test_names_shared_with_builtin_methods_stay() -> None:
    # `items` is also dict.items: renaming it would rename every call of the method.
    plan = make_plan(["items", "Items"], set())
    assert set(plan.tokens) == {"Items"}
    assert plan.kept["items"].startswith("kept in code: also a language")


def test_plan_skips_protected_names_and_avoids_collisions() -> None:
    plan = make_plan(["self", "len", "useState", "index", "__init__", "Thing"], set())
    assert set(plan.tokens) == {"Thing"}
    assert {"self", "len", "index"} <= PROTECTED
    assert set(plan.kept) == {"self", "len", "useState", "index", "__init__"}
    taken = make_plan(["Thing"], set()).words["thing"]
    # The generated word already exists in the repository: another one is drawn.
    other = make_plan(["Thing"], {taken.capitalize(), taken})
    assert other.words["thing"] != taken


def test_protected_python_names_are_frozen() -> None:
    # A literal list, not this interpreter's dir(builtins): the plan is the same on any Python.
    assert {"print", "len", "items", "keys", "match", "case", "ExceptionGroup"} <= PYTHON_NAMES
    source = (ROOT / "packages/evals/src/sensei_evals/python_names.py").read_text("utf-8")
    assert "dir(builtins)" in source  # only in the comment saying how it was generated
    assert "import builtins" not in source


def test_plans_are_deterministic_per_seed() -> None:
    names = ["alpha", "BetaGamma"]
    assert make_plan(names, set(), seed=1) == make_plan(names, set(), seed=1)
    assert make_plan(names, set(), seed=1).tokens != make_plan(names, set(), seed=2).tokens


def test_queries_keep_their_plain_language() -> None:
    plan = RenamePlan(seed=0, tokens={"flow": "devi", "run_flow": "vare_devi"})
    assert translate_query("where does a flow `run_flow`?", QueryClass.FLOW, plan) == (
        "where does a flow `vare_devi`?"
    )
    assert translate_query("run_flow flow", QueryClass.SYMBOL, plan) == "vare_devi devi"
    assert translate_query("def run_flow", QueryClass.REGEX, plan) == "def vare_devi"


def test_regex_escapes_are_boundaries_not_part_of_a_name() -> None:
    plan = RenamePlan(seed=0, tokens={"LoginService": "MufoKebe", "bLogin": "x", "d": "y"})
    regex = QueryClass.REGEX
    assert translate_query(r"\bLoginService\b", regex, plan) == r"\bMufoKebe\b"
    assert translate_query(r"\WLoginService\s*\(", regex, plan) == r"\WMufoKebe\s*\("
    assert translate_query(r"\d+LoginService", regex, plan) == r"\d+MufoKebe"
    # An escaped backslash is a literal: what follows it is a name again.
    assert translate_query(r"\\bLogin", regex, plan) == r"\\x"
    # Outside regex queries nothing changes.
    assert translate_query(r"\bLoginService", QueryClass.SYMBOL, plan) == r"\bLoginService"


def test_sub_replaces_whole_identifiers_only() -> None:
    plan = RenamePlan(seed=0, tokens={"item": "zev", "Item": "Zev"})
    text = 'item.items  Item(item_id) "/item/{id}" $item 0xitem # an item'
    assert plan.sub(text) == 'zev.items  Zev(item_id) "/zev/{id}" $item 0xitem # an zev'
    assert plan.path("src/item/Item.tsx") == "src/zev/Zev.tsx"


def test_names_are_renamed_only_in_the_languages_that_define_them() -> None:
    plan = make_plan(
        {"Mailer": {PYTHON}, "Widget": {JS}, "img": set[str](), "Shared": {PYTHON, JS}},
        set(),
        external={JS: {"Shared"}},
    )
    s, w, i, sh = (plan.tokens[n] for n in ("Mailer", "Widget", "img", "Shared"))
    text = "Mailer Widget img Shared"
    assert plan.sub(text, PY) == f"{s} Widget img {sh}"
    assert plan.sub(text, (JS,)) == f"Mailer {w} img Shared"
    assert plan.sub(text) == f"{s} {w} {i} {sh}"  # non-code files: every name
    assert plan.kept["Shared"] == "not renamed in js code: imported from a library there"
    only = make_plan({"Toaster": {JS}}, set(), external={JS: {"Toaster"}})
    assert only.tokens == {}
    assert only.kept["Toaster"] == "kept in js code: imported from a library there"


def test_paths_are_renamed_word_by_word() -> None:
    files = [
        "frontend/src/routes/recover-password.tsx",
        "backend/app/api/routes/items.py",
        "backend/app/api/routes/__init__.py",
        "frontend/src/routes/__root.tsx",
        "frontend/src/routeTree.gen.ts",
        "frontend/src/index.css",
        ".github/workflows/ci.yml",
    ]
    plan = make_plan(["items", "routes"], set(), paths=files)
    w = plan.words
    # The words the indexer reads roles from stay: api, routes (and route in routeTree).
    assert plan.kept["routes"].startswith("kept: made only of words the indexer reads")
    assert (w["api"], w["route"]) == ("api", "route")
    assert plan.path(files[0]) == f"{w['frontend']}/src/routes/" + (
        f"{w['recover']}-{w['password']}.tsx"
    )
    assert plan.path(files[1]) == f"{w['backend']}/app/api/routes/{w['items']}.py"
    assert plan.path(files[2]).endswith("/__init__.py")
    assert plan.path(files[3]).endswith("/__root.tsx")
    assert plan.path(files[4]).endswith(f"/src/{w['route']}{w['tree'].capitalize()}.gen.ts")
    assert plan.path(files[5]).endswith("/src/index.css")
    assert plan.path(files[6]) == f".github/{w['workflows']}/{w['ci']}.yml"
    # Specifiers rename the same way (no extension, ./ and ../ kept).
    assert plan.specifier("../routes/recover-password?raw") == (
        f"../routes/{w['recover']}-{w['password']}?raw"
    )


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def test_library_imports_are_collected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/models.py": (
                "from sqlmodel import Field, select as sel\nimport jwt\nimport os.path\n"
                "from app import helpers\nfrom . import other\nx = jwt.encode(1)\n"
            ),
            "app/helpers.py": "def h(): ...\n",
            "app/alembic/env.py": "from alembic import context\n",
            "web/a.tsx": (
                'import { Settings, Toaster as Sonner, type Icon } from "lucide-react"\n'
                'import * as P from "@radix-ui/react-select"\nimport D from "dayjs"\n'
                'import { mine } from "./b"\nimport { alias } from "@/c"\n'
                'const { pick: chosen } = require("lodash")\nP.Root\nD?.utc\n'
            ),
            "web/b.ts": "export const mine = 1\n",
        },
    )
    external = external_names(tmp_path)
    assert {"Field", "select", "sel", "jwt", "encode", "os", "context"} <= external[PYTHON]
    assert not {"helpers", "other", "h", "app", "alembic"} & external[PYTHON]
    assert {"Settings", "Toaster", "Sonner", "Icon", "P", "Root", "D", "utc"} <= external[JS]
    assert {"pick", "chosen"} <= external[JS]
    assert not {"mine", "alias", "type"} & external[JS]


def test_python_modules_know_their_roots() -> None:
    mods = PythonModules.of(
        [
            "backend/app/__init__.py",
            "backend/app/models.py",
            "backend/app/api/__init__.py",
            "backend/app/api/items.py",
            "backend/app/alembic/env.py",  # no __init__: a root of its own
            "scripts/tool.py",
        ]
    )
    assert {"app", "app.models", "app.api", "app.api.items", "app.alembic.env"} <= mods.dotted
    assert {"env", "tool", "scripts.tool"} <= mods.dotted
    assert "alembic" not in mods.dotted  # the library, not app/alembic
    assert mods.is_own(None, 1)
    assert mods.is_submodule("backend/app/main.py", "api", 1, "items")
    assert mods.is_submodule("backend/app/main.py", None, 1, "models")
    assert not mods.is_submodule("backend/app/main.py", None, 1, "settings")
    assert mods.is_submodule("x.py", "app.api", 0, "items")


def test_imports_follow_renamed_modules(tmp_path: Path) -> None:
    tree = _write(
        tmp_path / "src-tree",
        {
            "pkg/__init__.py": "",
            "pkg/items.py": "def read_items():\n    return []\n",
            "pkg/user_settings.py": "LIMIT = 1\n",
            "pkg/main.py": (
                "from pkg import items, user_settings as us\n"
                "from . import items as it\n"
                "from .items import read_items\n"
                "from pkg.items import (  # the reader\n"
                "    read_items as ri,\n"
                ")\n"
                "import pkg.user_settings\n"
                "import os.path\n"
                "from sqlmodel import select\n"
                "items.read_items()\n"
            ),
            "web/react/hooks.ts": "export const useThing = 1\n",
            "web/recover-password.tsx": "export function RecoverPassword() {}\n",
            "web/main.tsx": (
                'import { RecoverPassword } from "./recover-password"\n'
                'import { useThing } from "./react/hooks"\n'
                'import { Icon } from "lucide-react"\n'
                'const page = await import("./recover-password")\n'
            ),
        },
    )
    plan = plan_for(tree, labeling_symbols(tree))
    result = rename_tree(tree, tmp_path / "out", plan)
    t, items = plan.tokens, plan.segment("items")
    p, u, r = t["pkg"], t["user_settings"], t["read_items"]
    assert "items" in plan.kept  # protected in code...
    assert items != "items"  # ...but its file is renamed
    main = (tmp_path / "out" / result.paths["pkg/main.py"]).read_text(encoding="utf-8")
    assert main.splitlines() == [
        f"from {p} import {items} as items, {u} as us",
        f"from . import {items} as it",
        f"from .{items} import {r}",
        f"from {p}.{items} import (  # the reader",
        f"    {r} as ri,",
        ")",
        f"import {p}.{u}",
        "import os.path",
        "from sqlmodel import select",
        f"items.{r}()",
    ]
    assert result.paths["pkg/items.py"] == f"{p}/{items}.py"
    tsx = (tmp_path / "out" / result.paths["web/main.tsx"]).read_text(encoding="utf-8")
    kebab = plan.segment("recover-password")
    assert "react" in plan.tokens  # a directory name, yet the package keeps its name
    assert tsx.splitlines() == [
        f'import {{ {t["RecoverPassword"]} }} from "./{kebab}"',
        f'import {{ {t["useThing"]} }} from "./{t["react"]}/{plan.segment("hooks")}"',
        'import { Icon } from "lucide-react"',
        f'const {t["page"]} = await import("./{kebab}")',
    ]
    assert result.paths["web/recover-password.tsx"] == f"{plan.segment('web')}/{kebab}.tsx"


def test_ignore_and_attribute_files_follow_the_paths(tmp_path: Path) -> None:
    tree = _write(
        tmp_path / "src-tree",
        {
            ".gitignore": "# output of recover-password\n/web/recover-password.log\n*.png  \n",
            ".gitattributes": (
                "* text eol=lf\n*.png binary\nweb/recover-password.tsx\tlinguist-generated\n"
            ),
            "web/recover-password.tsx": "export const a = 1\r\nexport const b = 2\r\n",
        },
    )
    (tree / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    plan = plan_for(tree, labeling_symbols(tree))
    out = tmp_path / "out"
    result = rename_tree(tree, out, plan)
    web, kebab = plan.segment("web"), plan.segment("recover-password")
    assert (out / ".gitignore").read_text(encoding="utf-8").splitlines() == [
        "# output of recover-password",
        f"/{web}/{kebab}.log",
        "*.png  ",
    ]
    assert (out / ".gitattributes").read_text(encoding="utf-8").splitlines() == [
        "* text eol=lf",
        "*.png binary",
        f"{web}/{kebab}.tsx\tlinguist-generated",
    ]
    # The copy's attributes would turn CRLF into LF on commit: they don't get the chance.
    commit_tree(out)
    for path in result.paths.values():
        committed = subprocess.run(  # noqa: S603
            ["git", "-C", str(out), "show", f"HEAD:{path}"],  # noqa: S607
            check=True,
            capture_output=True,
        )
        assert committed.stdout == (out / path).read_bytes(), path
    status = subprocess.run(  # noqa: S603
        ["git", "-C", str(out), "status", "--porcelain"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "src-tree"
    (root / "web" / "Widgets").mkdir(parents=True)
    (root / "api").mkdir()
    (root / "web" / "Widgets" / "WidgetsTable.tsx").write_text(
        'import { readWidgets } from "../../api/widgets"\n\nexport function WidgetsTable() {\n'
        "  return readWidgets()\n}\n",
        encoding="utf-8",
    )
    (root / "api" / "widgets.py").write_text(
        'def read_widgets():\n    """Every widget."""\n    return []\n', encoding="utf-8"
    )
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    return root


def test_rename_tree(tree: Path, tmp_path: Path) -> None:
    names = ["Widgets", "WidgetsTable", "widgets", "readWidgets", "read_widgets"]
    plan = make_plan(names, set())
    result = rename_tree(tree, tmp_path / "out", plan)
    item, table = plan.words["widgets"], plan.tokens["WidgetsTable"]
    assert result.paths == {
        "api/widgets.py": f"api/{item}.py",
        "logo.png": "logo.png",
        "web/Widgets/WidgetsTable.tsx": f"web/{item.capitalize()}/{table}.tsx",
    }
    assert (result.rewritten, result.copied) == (2, 1)
    path = result.paths["web/Widgets/WidgetsTable.tsx"]
    tsx = (tmp_path / "out" / path).read_text(encoding="utf-8")
    assert f'from "../../api/{item}"' in tsx  # the import follows the renamed module
    assert plan.tokens["readWidgets"] in tsx
    assert "Widgets" not in tsx
    assert tsx.count("\n") == 5
    assert (tmp_path / "out" / "logo.png").read_bytes() == (tree / "logo.png").read_bytes()

    with pytest.raises(FileExistsError):
        rename_tree(tree, tmp_path / "out", plan)


def test_two_files_cannot_become_one(tree: Path, tmp_path: Path) -> None:
    (tree / "api" / "other.py").write_text("x = 1\n", encoding="utf-8")
    plan = RenamePlan(seed=0, tokens={"other": "widgets"})
    with pytest.raises(ValueError, match=r"would both become api/widgets\.py"):
        rename_tree(tree, tmp_path / "out", plan)


def test_commits_are_reproducible(tree: Path, tmp_path: Path) -> None:
    plan = make_plan(["widgets"], set())
    shas: list[str] = []
    for n in range(2):
        result = rename_tree(tree, tmp_path / f"out{n}", plan)
        shas.append(commit_tree(tmp_path / f"out{n}"))
        assert result.sha is None
    assert shas[0] == shas[1]
    assert len(shas[0]) == 40


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("template")
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(root, filter="data")
    return root / "fastapi-template"


@dataclass(frozen=True, slots=True)
class Copy:
    plan: RenamePlan
    result: RenameResult
    root: Path


@pytest.fixture(scope="module")
def renamed(template: Path, tmp_path_factory: pytest.TempPathFactory) -> Copy:
    out = tmp_path_factory.mktemp("renamed") / "copy"
    plan = plan_for(template, labeling_symbols(template))
    result = rename_tree(template, out, plan)
    result.sha = commit_tree(out)
    return Copy(plan, result, out)


def test_same_seed_same_sha_on_any_machine(
    template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A developer's git config must not change the copy's commit: autocrlf would rewrite
    # line endings, a global excludes file would leave files out.
    (tmp_path / "excludes").write_text("*.md\n*.tsx\n", encoding="utf-8")
    config = tmp_path / "gitconfig"
    config.write_text(
        f"[core]\n\tautocrlf = true\n\texcludesFile = {tmp_path / 'excludes'}\n"
        "[user]\n\tname = Someone Else\n\temail = else@example.com\n",
        encoding="utf-8",
    )
    shas: list[str] = []
    for n, hostile in enumerate((False, True, False)):
        with monkeypatch.context() as patch:
            if hostile:
                patch.setenv("GIT_CONFIG_GLOBAL", str(config))
                patch.setenv("GIT_AUTHOR_DATE", "2030-01-01T00:00:00Z")
            seed = 7 if n < 2 else 8
            plan = plan_for(template, labeling_symbols(template), seed=seed)
            result = rename_tree(template, tmp_path / f"out{n}", plan)
            shas.append(commit_tree(tmp_path / f"out{n}"))
            listed = subprocess.run(  # noqa: S603
                ["git", "-C", str(tmp_path / f"out{n}"), "ls-files"],  # noqa: S607
                check=True,
                capture_output=True,
                text=True,
            )
            assert len(listed.stdout.splitlines()) == len(result.paths)  # every file committed
    assert shas[0] == shas[1]
    assert shas[2] != shas[0]


def test_library_names_are_kept_at_their_call_sites(template: Path, renamed: Copy) -> None:
    def copied(path: str) -> str:
        return (renamed.root / renamed.result.paths[path]).read_text(encoding="utf-8")

    plan = renamed.plan
    # Python: sqlmodel's Field and select, pydantic's ValidationError.
    assert "from sqlmodel import Field, Relationship, SQLModel" in copied("backend/app/models.py")
    assert "from sqlmodel import Session, select" in copied("backend/app/crud.py")
    assert "select(" in copied("backend/app/crud.py")
    deps = copied("backend/app/api/deps.py")
    assert "from pydantic import ValidationError" in deps
    assert "except InvalidTokenError, ValidationError:" in deps
    # TypeScript: lucide-react's Settings and LucideIcon, sonner's Toaster.
    user = copied("frontend/src/components/Sidebar/User.tsx")
    assert 'import { ChevronsUpDown, LogOut, Settings } from "lucide-react"' in user
    assert "<Settings />" in user
    assert 'import type { LucideIcon } from "lucide-react"' in copied(
        "frontend/src/components/Sidebar/Main.tsx"
    )
    sonner = copied("frontend/src/components/ui/sonner.tsx")
    assert 'import { Toaster as Sonner, type ToasterProps } from "sonner"' in sonner
    # The repository's own Python Settings class keeps its name: settings is a role word.
    assert "class Settings(" in copied("backend/app/core/config.py")
    assert plan.kept["Settings"].startswith("kept: made only of words the indexer reads")
    # Names defined in TypeScript (the generated client's ValidationError type) stay there.
    assert plan.scopes["ValidationError"] == frozenset({JS})
    assert plan.tokens["ValidationError"] in copied("frontend/src/client/types.gen.ts")
    for name in ("Toaster", "LucideIcon"):
        assert name not in plan.tokens
        assert plan.kept[name] == "kept in js code: imported from a library there"


def test_kept_names_and_renamed_paths_are_in_the_mapping(renamed: Copy, tmp_path: Path) -> None:
    result = renamed.result
    renamed.result.repo = "copy"
    mapping = result.mapping()
    kept = cast("dict[str, str]", mapping["kept"])
    for name in ("items", "private", "index"):
        assert kept[name].startswith("kept in code: also a language")
    assert mapping["repo"] == "copy"
    assert mapping["scopes"]
    # Protected or kebab-case, file names don't give the repository away.
    w = renamed.plan.words
    assert result.paths["backend/app/api/routes/items.py"].endswith(f"/{w['items']}.py")
    assert result.paths["backend/app/api/routes/private.py"].endswith(f"/{w['private']}.py")
    assert result.paths["frontend/src/routes/recover-password.tsx"].endswith(
        f"/{w['recover']}-{w['password']}.tsx"
    )
    for old, new in result.paths.items():
        words = {part.lower() for part in split_words(new)}
        assert not {"items", "login", "recover", "password", "users"} & words, (old, new)
    # It round-trips into a result that translates gold the same way.
    again = RenameResult.from_mapping(json.loads(json.dumps(mapping)))
    assert again.plan == renamed.plan
    assert again.paths == result.paths


def test_python_imports_resolve_in_the_copy(template: Path, renamed: Copy) -> None:
    paths_ = renamed.result.paths
    before, after = PythonModules.of(paths_), PythonModules.of(paths_.values())
    plan = renamed.plan
    checked = 0
    for old, new in paths_.items():
        if language_of(old) != PYTHON:
            continue
        a, b = parse_python(read_text(template, old)), parse_python(read_text(renamed.root, new))
        assert a is not None
        assert b is not None
        ours = [n for n in ast.walk(a) if isinstance(n, ast.Import | ast.ImportFrom)]
        theirs = [n for n in ast.walk(b) if isinstance(n, ast.Import | ast.ImportFrom)]
        assert len(ours) == len(theirs)
        for x, y in zip(ours, theirs, strict=True):
            module_x = x.module if isinstance(x, ast.ImportFrom) else None
            module_y = y.module if isinstance(y, ast.ImportFrom) else None
            level = x.level if isinstance(x, ast.ImportFrom) else 0
            own = isinstance(x, ast.ImportFrom) and before.is_own(module_x, level)
            for ax, ay in zip(x.names, y.names, strict=True):
                if isinstance(x, ast.Import):
                    if before.is_own(ax.name, 0):
                        assert ay.name in after.dotted, (new, ay.name)
                        checked += 1
                    else:
                        assert ay.name == ax.name
                    continue
                if not own:
                    assert (ay.name, ay.asname) == (ax.name, ax.asname)  # a library: untouched
                    continue
                checked += 1
                if level == 0:
                    assert module_y in after.dotted, (new, module_y)
                # The name the code uses is the one the rest of the file was renamed to.
                assert (ay.asname or ay.name) == plan.sub(ax.asname or ax.name, PY)
                if before.is_submodule(old, module_x, level, ax.name):
                    assert after.is_submodule(new, module_y, level, ay.name), (new, ay.name)
    assert checked > 50


_SPECIFIER = re.compile(
    r"""(?:\bfrom|\bimport|\bimport\s*\()\s*["'](?P<spec>\.{1,2}/[^"']*|@/[^"']*)["']"""
)
_EXTENSIONS = ("", ".ts", ".tsx", ".js", ".jsx", ".d.ts", "/index.ts", "/index.tsx", "/index.js")


def _resolves(root: Path, file: str, spec: str, alias: Path) -> bool:
    spec = spec.partition("?")[0]
    base = alias / spec[2:] if spec.startswith("@/") else (root / file).parent / spec
    return any(Path(f"{base}{extension}").is_file() for extension in _EXTENSIONS)


def test_typescript_imports_resolve_in_the_copy(template: Path, renamed: Copy) -> None:
    alias_before = template / "frontend" / "src"
    alias_after = renamed.root / renamed.plan.path("frontend/src")
    resolved = 0
    for old, new in renamed.result.paths.items():
        if language_of(old) != JS:
            continue
        ours = [m["spec"] for m in _SPECIFIER.finditer(read_text(template, old))]
        theirs = [m["spec"] for m in _SPECIFIER.finditer(read_text(renamed.root, new))]
        assert len(ours) == len(theirs)
        for x, y in zip(ours, theirs, strict=True):
            if _resolves(template, old, x, alias_before):
                assert _resolves(renamed.root, new, y, alias_after), (new, y)
                resolved += 1
    assert resolved > 100


TARGETS = [
    GoldTarget(
        layer=Layer.UI, path="frontend/src/routes/login.tsx", symbol="Login", lines=(53, 142)
    ),
    GoldTarget(
        layer=Layer.CLIENT,
        path="frontend/src/client/sdk.gen.ts",
        symbol="LoginService.loginAccessToken",
        lines=(27, 38),
    ),
    GoldTarget(
        layer=Layer.HANDLER,
        path="backend/app/api/routes/login.py",
        symbol="login_access_token",
        lines=(23, 42),
    ),
    GoldTarget(layer=Layer.DB, path="backend/app/models.py"),
]


def _suite(repo: str = "fastapi-template", sha: str = SHA) -> Suite:
    item = GoldenItem(
        id="ft-flow-login",
        repo=repo,
        sha=sha,
        query="what happens when `login_access_token` gets a wrong password",
        class_=QueryClass.FLOW,
        layers=[Layer.UI, Layer.CLIENT, Layer.HANDLER, Layer.DB],
        gold=TARGETS,
    )
    regex = GoldenItem(
        id="ft-regex-login",
        repo=repo,
        sha=sha,
        query=r"\bLoginService\b",
        class_=QueryClass.REGEX,
        layers=[Layer.CLIENT],
        gold=[TARGETS[1]],
    )
    return Suite(name="ft", version=3, items=[item, regex])


def test_gold_follows_the_copy(template: Path, renamed: Copy) -> None:
    plan, result, copy = renamed.plan, renamed.result, renamed.root
    translated = translate_suite(_suite(), "fastapi-template", result, new_repo="ft-renamed")
    item, regex = translated.items
    assert result.sha is not None
    assert (item.id, item.repo, item.sha) == ("ft-flow-login.renamed", "ft-renamed", result.sha)
    assert plan.tokens["login_access_token"] in item.query
    assert "login_access_token" not in item.query
    assert regex.query == rf"\b{plan.tokens['LoginService']}\b"
    for before, after in zip(TARGETS, item.gold, strict=True):
        assert after.lines == before.lines
        assert after.path != before.path  # every gold file sits under a renamed name
        if before.lines is None:
            continue
        start, end = before.lines
        old = read_lines(template, before.path)[start - 1 : end]
        new = read_lines(copy, after.path)[start - 1 : end]
        language = language_of(before.path)
        assert language is not None
        assert new == [plan.sub(line, (language,)) for line in old]
        assert new != old
    # The translated gold checks out against the copy, with exact Python spans.
    pinned = {"ft-renamed": PinnedRepo(id="ft-renamed", url="file://copy", sha=result.sha)}
    problems = validate_suite("ft-renamed", translated, pinned, lambda _: copy)
    assert [p.render() for p in problems if not p.warning] == []
    # And it round-trips through the golden file format.
    assert parse_suite(suite_yaml(translated)) == translated


def test_translate_needs_a_commit_and_known_paths(template: Path, tmp_path: Path) -> None:
    result = rename_tree(template, tmp_path / "copy", RenamePlan(seed=0, tokens={}))
    with pytest.raises(ValueError, match="commit the renamed copy"):
        translate_suite(_suite(), "fastapi-template", result, new_repo="x")
    result.sha = "0" * 40
    missing = (
        _suite()
        .items[0]
        .model_copy(
            update={"gold": [GoldTarget(layer=Layer.UI, path="nope.tsx")], "layers": [Layer.UI]}
        )
    )
    with pytest.raises(TranslateError, match=r"item ft-flow-login: gold path nope\.tsx is not"):
        translate_suite(
            Suite(name="s", version=0, items=[missing]), "fastapi-template", result, new_repo="x"
        )


def _rename(*args: str | Path) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["rename", *(str(arg) for arg in args)])
    return result.exit_code, result.output


def test_rename_command(template: Path, tmp_path: Path) -> None:
    suite_file = tmp_path / "ft.yaml"
    suite_file.write_text(suite_yaml(_suite()), encoding="utf-8")
    out = tmp_path / "copies" / "my-copy"
    code, output = _rename(
        "--suite", suite_file, "--repo", "fastapi-template", "--checkout", template,
        "--out", out, "--suite-out", tmp_path / "ft-renamed.yaml",
    )  # fmt: skip
    assert code == 0, output
    mapping_file = mapping_path(tmp_path / "copies", "my-copy")
    assert mapping_file.is_file()  # beside the copy, not inside it
    assert not (out / mapping_file.name).exists()
    mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
    translated = parse_suite((tmp_path / "ft-renamed.yaml").read_text(encoding="utf-8"))
    assert translated.name == "ft-renamed"
    # The copy's id is its folder's name, in the mapping and the suite alike.
    assert mapping["repo"] == "my-copy"
    assert mapping["names"] == "extracted"  # the indexer's symbol table by default
    assert "names renamed from extracted" in output
    assert {item.repo for item in translated.items} == {"my-copy"}
    assert translated.items[0].sha == paths.head_sha(out) == mapping["sha"]
    assert translated.renamed_from is not None
    assert translated.renamed_from.suite == "ft"
    assert mapping["source"] == {
        "repo": "fastapi-template",
        "sha": SHA,
        "suite": "ft",
        "suite_sha256": translated.renamed_from.sha256,
    }
    code, output = _rename(
        "--suite", suite_file, "--repo", "fastapi-template", "--checkout", template,
        "--out", tmp_path / "Bad Name",
    )  # fmt: skip
    assert code == 1
    assert "--out must end in a folder named like a repository id" in output


ValidateFn = Callable[..., int]


def _label_helper_validate() -> ValidateFn:
    spec = importlib.util.spec_from_file_location("label_helper", ROOT / "scripts/label_helper.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast("ValidateFn", module.validate)


def _commit(directory: Path) -> str:
    git = ["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "snapshot"]):
        subprocess.run([*git, *args], check=True)  # noqa: S603
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)  # noqa: S603
    return head.stdout.strip()


@dataclass(frozen=True, slots=True)
class PrivateRoot:
    root: Path
    source: Path  # the source suite
    renamed: Path  # the renamed copy's suite
    checkouts: Path
    sha: str


@pytest.fixture
def private_root(template: Path, tmp_path: Path) -> PrivateRoot:
    """A repository root with the private files: a pinned checkout of the template, a suite
    for it, and a renamed copy of both made by ``sensei-eval rename``."""
    root = tmp_path / "root"
    golden, checkouts = paths.golden_dir(root), paths.checkouts_dir(root)
    golden.mkdir(parents=True)
    checkouts.mkdir(parents=True)
    shutil.copytree(template, checkouts / "tpl", symlinks=True)
    sha = _commit(checkouts / "tpl")
    paths.repos_file(root).write_text(
        f"repos:\n  - id: tpl\n    url: https://example.com/tpl\n    sha: {sha}\n", encoding="utf-8"
    )
    source = golden / "tpl.yaml"
    source.write_text(suite_yaml(_suite("tpl", sha)), encoding="utf-8")
    renamed_suite = golden / "tpl-renamed.yaml"
    code, output = _rename(
        "--suite", source, "--repo", "tpl", "--checkout", checkouts / "tpl",
        "--out", checkouts / "tpl-renamed", "--suite-out", renamed_suite,
    )  # fmt: skip
    assert code == 0, output
    return PrivateRoot(root, source, renamed_suite, checkouts, sha)


def test_label_helper_catches_stale_copies(
    private_root: PrivateRoot, capsys: pytest.CaptureFixture[str]
) -> None:
    validate = _label_helper_validate()
    source, renamed_suite = private_root.source, private_root.renamed

    def run() -> tuple[int, str]:
        code = validate([], False, root=private_root.root)
        return code, capsys.readouterr().out

    code, out = run()
    assert code == 0, out
    assert "2 suites, 4 items: 0 errors" in out

    # The source suite is edited after the copy was made.
    original = source.read_text(encoding="utf-8")
    source.write_text(original.replace("wrong password", "bad password"), encoding="utf-8")
    code, out = run()
    assert code == 1
    assert "tpl-renamed.yaml: renamed_from: error: stale renamed suite: tpl.yaml changed" in out
    assert "doesn't reproduce it (items ft-flow-login.renamed differ): re-run rename" in out
    source.write_text(original, encoding="utf-8")

    # The mapping says the copy was made from another commit.
    mapping_file = mapping_path(private_root.checkouts, "tpl-renamed")
    mapping_text = mapping_file.read_text(encoding="utf-8")
    mapping = json.loads(mapping_text)
    mapping["source"]["sha"] = "2" * 40
    mapping_file.write_text(json.dumps(mapping), encoding="utf-8")
    code, out = run()
    assert code == 1
    assert f"the copy was made from tpl at {'2' * 40}, the source suite tpl.yaml is pinned" in out
    mapping_file.write_text(mapping_text, encoding="utf-8")

    # A renamed suite written by an older rename (no `renamed_from`) can't be trusted.
    fresh = renamed_suite.read_text(encoding="utf-8")
    old = parse_suite(fresh).model_copy(update={"renamed_from": None})
    renamed_suite.write_text(suite_yaml(old), encoding="utf-8")
    code, out = run()
    assert code == 1
    assert "without `renamed_from` (written by an older rename" in out
    renamed_suite.write_text(fresh, encoding="utf-8")

    # A local edit in the source checkout is caught too.
    scratch = private_root.checkouts / "tpl" / "scratch.py"
    scratch.write_text("x = 1\n", encoding="utf-8")
    code, out = run()
    assert code == 1
    assert "has local changes (git status isn't clean)" in out
    scratch.unlink()
    assert run()[0] == 0


def test_label_helper_reports_a_failed_fetch(
    private_root: PrivateRoot, capsys: pytest.CaptureFixture[str]
) -> None:
    # A failed fetch is a problem for that repository; the other suites are still checked.
    shutil.rmtree(private_root.checkouts / "tpl")
    paths.repos_file(private_root.root).write_text(
        f"repos:\n  - id: tpl\n    url: https://127.0.0.1:9/nowhere\n    sha: {private_root.sha}\n",
        encoding="utf-8",
    )
    code = _label_helper_validate()([], True, root=private_root.root)
    out = capsys.readouterr().out
    assert code == 1
    assert "tpl.yaml: ft-flow-login: error: fetching tpl into" in out
    assert "2 suites, 4 items: 1 errors" in out  # the renamed copy's suite still passes


def test_label_helper_finds_copies_made_elsewhere(
    template: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    golden = tmp_path / "golden"
    golden.mkdir()
    source = golden / "ft.yaml"
    source.write_text(suite_yaml(_suite()), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    code, output = _rename(
        "--suite", source, "--repo", "fastapi-template", "--checkout", template,
        "--out", elsewhere / "ft-copy", "--suite-out", golden / "ft-renamed.yaml",
    )  # fmt: skip
    assert code == 0, output
    validate = _label_helper_validate()
    code = validate([golden / "ft-renamed.yaml"], False, root=tmp_path, checkouts=elsewhere)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "1 suites, 2 items: 0 errors" in out


def _definitions(root: Path) -> dict[str, list[str]]:
    """Every definition the indexer's extractor finds under ``root``: name → paths."""
    found: dict[str, list[str]] = {}
    for file in sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()):
        if file.startswith(".git/"):
            continue
        data = read_source(root, file)
        language = detect_language(file, data[:256])
        if language is None or not parseable(language):
            continue
        for symbol in extract_symbols(parse_file(file, data, language=language), language):
            if symbol.kind is not SymbolKind.MODULE:
                found.setdefault(symbol.name, []).append(file)
    return found


def test_tree_sitter_symbols_rename_every_definition(template: Path, tmp_path: Path) -> None:
    """With the 2.4 symbol table as the source, every definition is renamed: nested functions,
    methods, React components and hooks declared inside components, not only what the labeling
    parsers find. No symbol-table name survives in the copy unless the mapping says why."""
    names = extracted_symbols(template)
    labeled = labeling_symbols(template)
    assert set(labeled) <= set(names)
    # Handlers and helpers declared inside components and hooks, and functions nested in
    # functions: the labeling parsers only see top-level declarations and class methods.
    for name in ("handleLogout", "getInitialOpen", "showErrorToast", "abortHandler"):
        assert name in names, name
        assert name not in labeled, name
    assert names["handleLogout"] == frozenset({JS})
    # Keys that aren't identifiers, and the library interfaces `declare module` augments.
    assert not {"'/login'", "200", "Register", "FileRoutesByPath"} & set(names)

    plan = plan_for(template, names)
    out = tmp_path / "copy"
    rename_tree(template, out, plan)
    before = _definitions(template)
    after = _definitions(out)
    survivors = {
        name: files for name, files in after.items() if name in names and name not in plan.kept
    }
    assert not survivors, survivors
    # What survives is kept for a reason the mapping gives: a language or framework name, or
    # an import from a library.
    kept = {name for name in after if name in before and name in plan.kept}
    assert {"items", "children", "className"} <= kept
    assert all(plan.kept[name] for name in kept)
    # Every definition is still there under its new name, file for file.
    assert sum(map(len, after.values())) == sum(map(len, before.values()))
    for name in ("handleLogout", "getInitialOpen", "SidebarProvider", "useAuth"):
        assert plan.tokens[name] in after, name
        assert name not in after, name


def test_every_file_keeps_its_role_and_the_frameworks_stay(template: Path, tmp_path: Path) -> None:
    """The copy is indexed as its original is: the same role, language and flags per file, and
    the same frameworks at the renamed package roots (manifests are copied unchanged)."""
    plan = plan_for(template, extracted_symbols(template))
    out = tmp_path / "copy"
    result = rename_tree(template, out, plan)
    before, after = discover(template, max_files=10_000), discover(out, max_files=10_000)
    copied = {file.path: file for file in after.files}
    changed = [
        (file.path, file.role.value, copy.role.value)
        for file in before.files
        if (copy := copied[result.paths[file.path]]).role != file.role
        or (copy.language, copy.flags) != (file.language, file.flags)
    ]
    assert not changed, changed[:10]

    def frameworks(found: Discovery, rename: Callable[[str], str]) -> set[tuple[str, str]]:
        return {(rename(f.root), f.name) for f in found.frameworks}

    assert frameworks(before, lambda root: root if root == "." else plan.path(root)) == (
        frameworks(after, lambda root: root)
    )
    assert {name for _, name in frameworks(after, str)} >= {"fastapi", "react", "sqlmodel"}
    manifest = "frontend/package.json"
    assert (out / result.paths[manifest]).read_bytes() == (template / manifest).read_bytes()
    # A hook keeps its prefix, so it is still a hook.
    assert plan.tokens["useAuth"].startswith("use")
    assert plan.tokens["useAuth"] != "useAuth"
