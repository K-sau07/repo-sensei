"""Renamed copies: one consistent mapping for names, paths and gold, with lines unchanged."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sensei_evals import paths
from sensei_evals.cli import app
from sensei_evals.golden import read_lines, validate_suite
from sensei_evals.rename import (
    PROTECTED,
    RenamePlan,
    commit_tree,
    labeling_symbols,
    make_plan,
    rename_tree,
    repository_tokens,
    split_words,
    suite_yaml,
    translate_query,
    translate_suite,
)
from sensei_evals.repos import PinnedRepo
from sensei_evals.schema import GoldenItem, GoldTarget, Layer, QueryClass, Suite, parse_suite

FIXTURE = (
    Path(__file__).parents[3]
    / "packages"
    / "engine"
    / "tests"
    / "fixtures"
    / "fastapi-template.tar.gz"
)
SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e"


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


def test_plan_skips_protected_names_and_avoids_collisions() -> None:
    plan = make_plan(["self", "len", "useState", "index", "__init__", "Thing"], set())
    assert set(plan.tokens) == {"Thing"}
    assert {"self", "len", "index"} <= PROTECTED
    taken = make_plan(["Thing"], set()).words["thing"]
    # The generated word already exists in the repository: another one is drawn.
    other = make_plan(["Thing"], {taken.capitalize(), taken})
    assert other.words["thing"] != taken


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


def test_sub_replaces_whole_identifiers_only() -> None:
    plan = RenamePlan(seed=0, tokens={"item": "zev", "Item": "Zev"})
    text = 'item.items  Item(item_id) "/item/{id}" $item 0xitem # an item'
    assert plan.sub(text) == 'zev.items  Zev(item_id) "/zev/{id}" $item 0xitem # an zev'
    assert plan.path("src/item/Item.tsx") == "src/zev/Zev.tsx"


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


def _suite() -> Suite:
    item = GoldenItem(
        id="ft-flow-login",
        repo="fastapi-template",
        sha=SHA,
        query="what happens when `login_access_token` gets a wrong password",
        class_=QueryClass.FLOW,
        layers=[Layer.UI, Layer.CLIENT, Layer.HANDLER, Layer.DB],
        gold=TARGETS,
    )
    return Suite(name="ft", version=3, items=[item])


def test_gold_follows_the_copy(template: Path, tmp_path: Path) -> None:
    plan = make_plan(labeling_symbols(template), repository_tokens(template))
    result = rename_tree(template, tmp_path / "copy", plan)
    result.sha = commit_tree(tmp_path / "copy")
    translated = translate_suite(_suite(), "fastapi-template", result, new_repo="ft-renamed")
    [item] = translated.items
    assert (item.id, item.repo, item.sha) == ("ft-flow-login.renamed", "ft-renamed", result.sha)
    assert plan.tokens["login_access_token"] in item.query
    assert "login_access_token" not in item.query
    for before, after in zip(TARGETS, item.gold, strict=True):
        assert after.lines == before.lines
        assert after.path != before.path  # every gold file sits under a renamed name
        if before.lines is None:
            continue
        start, end = before.lines
        old = read_lines(template, before.path)[start - 1 : end]
        new = read_lines(tmp_path / "copy", after.path)[start - 1 : end]
        assert new == [plan.sub(line) for line in old]
        assert new != old
    # The translated gold checks out against the copy, with exact Python spans.
    pinned = {"ft-renamed": PinnedRepo(id="ft-renamed", url="file://copy", sha=result.sha)}
    problems = validate_suite("ft-renamed", translated, pinned, lambda _: tmp_path / "copy")
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
    with pytest.raises(KeyError, match=r"nope\.tsx"):
        translate_suite(
            Suite(name="s", version=0, items=[missing]), "fastapi-template", result, new_repo="x"
        )


def test_rename_command(template: Path, tmp_path: Path) -> None:
    suite_file = tmp_path / "ft.yaml"
    suite_file.write_text(suite_yaml(_suite()), encoding="utf-8")
    out = tmp_path / "copies" / "ft-renamed"
    result = CliRunner().invoke(
        app,
        [
            "rename", "--suite", str(suite_file), "--repo", "fastapi-template",
            "--checkout", str(template), "--out", str(out),
            "--suite-out", str(tmp_path / "ft-renamed.yaml"),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert (tmp_path / "copies" / "ft-renamed.mapping.json").is_file()  # beside, not inside
    assert not (out / "ft-renamed.mapping.json").exists()
    translated = parse_suite((tmp_path / "ft-renamed.yaml").read_text(encoding="utf-8"))
    assert translated.name == "ft-renamed"
    assert translated.items[0].sha == paths.head_sha(out)


@pytest.mark.skip(reason="TODO(2.4): enable once tree-sitter symbol extraction lands")
def test_tree_sitter_symbols_rename_every_definition(template: Path) -> None:  # pragma: no cover
    """With the 2.4 symbol table as the source, every definition is renamed: nested functions,
    methods, React components and hooks declared inside components, not only what the labeling
    parsers find. Swap the source for the 2.4 extractor and assert that no symbol-table name
    (other than protected ones) survives in the copy."""
    raise NotImplementedError
