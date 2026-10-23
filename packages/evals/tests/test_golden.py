"""Golden validation and span lookup, on small files and on the pinned fastapi template fixture
(whose spans were read off the files at that commit)."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path
from textwrap import dedent

import pytest

from sensei_evals.golden import check_target, find_symbol, symbol_at, symbols_in, validate_suite
from sensei_evals.repos import PinnedRepo, load_pinned
from sensei_evals.schema import GoldenItem, GoldTarget, Layer, QueryClass, Suite

FIXTURE = (
    Path(__file__).parents[3]
    / "packages"
    / "engine"
    / "tests"
    / "fixtures"
    / "fastapi-template.tar.gz"
)
SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e"


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("template")
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(root, filter="data")
    return root / "fastapi-template"


def _spans(checkout: Path, path: str) -> dict[str, tuple[int, int]]:
    return {span.name: span.lines for span in symbols_in(checkout, path)}


def test_python_spans_include_decorators(template: Path) -> None:
    spans = _spans(template, "backend/app/api/routes/login.py")
    assert spans["router"] == (20, 20)
    assert spans["login_access_token"] == (23, 42)  # from @router.post(...) to the return
    assert spans["recover_password_html_content"] == (100, 123)
    assert all(span.exact for span in symbols_in(template, "backend/app/api/routes/login.py"))


def test_python_methods_are_qualified(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        dedent(
            """\
            class Box:
                size = 1

                @property
                def area(self):
                    def inner():
                        return 2
                    return inner()
            """
        ),
        encoding="utf-8",
    )
    assert _spans(tmp_path, "m.py") == {"Box": (1, 8), "Box.area": (4, 8), "Box.area.inner": (6, 7)}
    found = symbol_at(tmp_path, "m.py", 7)
    assert found is not None
    assert found.name == "Box.area.inner"


def test_typescript_spans_without_semicolons(template: Path) -> None:
    spans = _spans(template, "frontend/src/routes/login.tsx")
    assert spans == {
        "formSchema": (25, 31),
        "FormData": (33, 33),
        "Route": (35, 51),
        "Login": (53, 142),
    }
    assert not any(span.exact for span in symbols_in(template, "frontend/src/routes/login.tsx"))


def test_typescript_class_methods(template: Path) -> None:
    path = "frontend/src/client/sdk.gen.ts"
    [span] = find_symbol(template, path, "LoginService.loginAccessToken")
    assert span.lines == (27, 38)
    assert _spans(template, path)["LoginService"] == (21, 97)


def test_typescript_semicolons_and_multiline_types(tmp_path: Path) -> None:
    (tmp_path / "a.ts").write_text(
        dedent(
            """\
            export const A = 1;
            type B =
              | "x"
              | "y";
            export default function c(x: number): string {
              const s = `}${x}{`;
              return s;
            }
            const d = items
              .map((i) => i)
              .filter(Boolean)
            """
        ),
        encoding="utf-8",
    )
    assert _spans(tmp_path, "a.ts") == {"A": (1, 1), "B": (2, 4), "c": (5, 8), "d": (9, 11)}
    assert symbols_in(tmp_path, "notes.md") == []


def _target(**overrides: object) -> GoldTarget:
    values: dict[str, object] = {
        "layer": Layer.HANDLER,
        "path": "backend/app/api/routes/login.py",
        "symbol": "login_access_token",
        "lines": (23, 42),
    }
    values.update(overrides)
    return GoldTarget.model_validate(values)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({}, None),
        ({"symbol": None, "lines": None}, None),
        ({"path": "backend/app/api/routes/logins.py"}, "no such file"),
        ({"symbol": None, "lines": (120, 125)}, "run past the end (line 123)"),
        ({"lines": (22, 42)}, "login_access_token is at lines 23-42, the gold says 22-42"),
        ({"symbol": "login_token"}, "symbol login_token not found"),
    ],
)
def test_check_target(template: Path, overrides: dict[str, object], error: str | None) -> None:
    errors, warnings = check_target(template, _target(**overrides))
    assert warnings == []
    if error is None:
        assert errors == []
    else:
        [message] = errors
        assert error in message


def test_heuristic_disagreements_are_warnings(template: Path) -> None:
    ui = {"layer": Layer.UI, "path": "frontend/src/routes/login.tsx"}
    errors, warnings = check_target(template, _target(**ui, symbol="Login", lines=(53, 140)))
    assert errors == []
    assert warnings == [
        "frontend/src/routes/login.tsx: Login is at lines 53-142, the gold says 53-140"
    ]
    # A name the heuristic doesn't declare (a variable inside a component) only needs to be there.
    errors, warnings = check_target(template, _target(**ui, symbol="loginMutation", lines=(53, 60)))
    assert errors == []
    assert "check lines 53-60 by hand" in warnings[0]
    errors, _ = check_target(template, _target(**ui, symbol="nothingLikeThis", lines=(53, 60)))
    assert "isn't in lines 53-60" in errors[0]


def _git_checkout(source: Path, directory: Path) -> str:
    subprocess.run(["cp", "-R", f"{source}/.", str(directory)], check=True)  # noqa: S603, S607
    git = ["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "snapshot"]):
        subprocess.run([*git, *args], check=True)  # noqa: S603
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)  # noqa: S603
    return head.stdout.strip()


def test_validate_suite(template: Path, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    sha = _git_checkout(template, checkout)
    item = GoldenItem(
        id="ok-1",
        repo="tpl",
        sha=sha,
        query="where are tokens issued",
        class_=QueryClass.CONCEPTUAL,
        layers=[Layer.HANDLER],
        gold=[_target()],
    )
    broken = item.model_copy(update={"id": "bad-1", "query": "q2", "gold": [_target(lines=(1, 2))]})
    other = item.model_copy(update={"id": "bad-2", "query": "q3", "sha": SHA})
    unpinned = item.model_copy(update={"id": "bad-3", "query": "q4", "repo": "nope"})
    suite = Suite(name="v", version=0, items=[item, broken, other, unpinned])
    pinned = {"tpl": PinnedRepo(id="tpl", url="https://example.com/tpl", sha=sha)}

    problems = validate_suite("v.yaml", suite, pinned, lambda repo: checkout)
    assert [(p.item, p.warning) for p in problems] == [
        ("bad-1", False),
        ("bad-2", False),
        ("bad-3", False),
    ]
    assert "is at lines 23-42, the gold says 1-2" in problems[0].message
    assert problems[1].message == f"sha {SHA} is not the pinned {sha}"
    assert problems[2].message == "repository nope is not pinned"
    assert problems[0].render().startswith("v.yaml: bad-1: error: ")

    # A checkout at another commit can't vouch for anything.
    stale = {"tpl": PinnedRepo(id="tpl", url="https://example.com/tpl", sha="1" * 40)}
    renamed = suite.model_copy(update={"items": [item.model_copy(update={"sha": "1" * 40})]})
    [problem] = validate_suite("v.yaml", renamed, stale, lambda repo: checkout)
    assert problem.message == f"checkout {checkout} is not at {'1' * 40}"


def test_load_pinned(tmp_path: Path) -> None:
    assert load_pinned(tmp_path / "missing.yaml") == {}
    path = tmp_path / "repos.yaml"
    path.write_text(
        f"version: 1\nrepos:\n  - id: tpl\n    url: https://example.com/tpl\n    sha: {SHA}\n"
        "    license: MIT\n",
        encoding="utf-8",
    )
    assert load_pinned(path)["tpl"].sha == SHA


def test_python_314_except_syntax_still_gets_exact_spans(tmp_path: Path) -> None:
    # PEP 758 (Python 3.14) drops the parentheses; older parsers need them back, same lines.
    (tmp_path / "deps.py").write_text(
        "def decode(token):\n    try:\n        return int(token)\n"
        "    except ValueError, TypeError:\n        return None\n\n\nx = 1\n",
        encoding="utf-8",
    )
    spans = symbols_in(tmp_path, "deps.py")
    assert [(s.name, s.lines, s.exact) for s in spans] == [
        ("decode", (1, 5), True),
        ("x", (8, 8), True),
    ]


def test_unparseable_python_falls_back_to_indentation(tmp_path: Path) -> None:
    (tmp_path / "new.py").write_text(
        dedent(
            '''\
            @router.post(
                "/x",
            )
            def handler(
                a,
            ) -> int:
                """Doc

            text at column zero
                """
                return a  # not valid in any parser: the next line is
                match := := 1

            ITEMS = [
                1,
            ]
            '''
        ),
        encoding="utf-8",
    )
    spans = {s.name: (s.lines, s.exact) for s in symbols_in(tmp_path, "new.py")}
    assert spans == {"handler": ((1, 12), False), "ITEMS": ((14, 16), False)}
