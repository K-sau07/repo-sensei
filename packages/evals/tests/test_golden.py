"""Golden validation and span lookup, on small files and on the pinned fastapi template fixture
(whose spans were read off the files at that commit)."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path
from textwrap import dedent

import pytest

from sensei_evals.golden import (
    CheckoutError,
    Problem,
    check_mix,
    check_target,
    find_symbol,
    indexed_paths,
    read_lines,
    split_lines,
    symbol_at,
    symbols_in,
    validate_suite,
)
from sensei_evals.repos import (
    PinnedRepo,
    fetch_checkout,
    load_pinned,
    load_renamed,
    mapping_path,
)
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
    assert _spans(tmp_path, "m.py") == {
        "Box": (1, 8),
        "Box.size": (2, 2),
        "Box.area": (4, 8),
        "Box.area.inner": (6, 7),
    }
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

    problems = _errors(validate_suite("v.yaml", suite, pinned, lambda repo: checkout))
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
    [problem] = _errors(validate_suite("v.yaml", renamed, stale, lambda repo: checkout))
    assert problem.message == f"checkout {checkout} is not at {'1' * 40}"

    # Nor can one at the pinned commit with a local edit, or an untracked file.
    ok = Suite(name="v", version=0, items=[item])
    login = checkout / "backend/app/api/routes/login.py"
    original = login.read_bytes()
    login.write_bytes(original + b"# edited\n")
    [problem] = _errors(validate_suite("v.yaml", ok, pinned, lambda repo: checkout))
    assert problem.message == f"checkout {checkout} has local changes (git status isn't clean)"
    login.write_bytes(original)
    assert _errors(validate_suite("v.yaml", ok, pinned, lambda repo: checkout)) == []
    (checkout / "scratch.txt").write_text("notes\n", encoding="utf-8")
    [problem] = _errors(validate_suite("v.yaml", ok, pinned, lambda repo: checkout))
    assert "has local changes" in problem.message


def _errors(problems: list[Problem]) -> list[Problem]:
    return [problem for problem in problems if not problem.warning]


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


def test_python_definitions_in_blocks_and_class_fields(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        dedent(
            """\
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                from x import Y
                Alias = int
            else:
                Alias = str
            try:
                import fast as engine
            except ImportError:
                def engine():
                    return None
            else:
                READY = True
            finally:
                DONE = True
            with open("f") as f:
                CONFIG = f.read()
            match engine:
                case None:
                    MODE = "slow"


            class M:
                email: str
                name = "m"
                if TYPE_CHECKING:
                    hidden: int = 0

                def f(self):
                    if True:
                        local = 1
                    return local
            """
        ),
        encoding="utf-8",
    )
    spans = [(span.name, span.lines) for span in symbols_in(tmp_path, "m.py")]
    assert spans == [
        ("Alias", (5, 5)),
        ("Alias", (7, 7)),
        ("engine", (11, 12)),
        ("READY", (14, 14)),
        ("DONE", (16, 16)),
        ("CONFIG", (18, 18)),
        ("MODE", (21, 21)),
        ("M", (24, 33)),
        ("M.email", (25, 25)),
        ("M.name", (26, 26)),
        ("M.hidden", (28, 28)),
        ("M.f", (30, 33)),
    ]
    # A name defined twice (a fallback) matches either definition.
    target = GoldTarget(layer=Layer.DB, path="m.py", symbol="Alias", lines=(7, 7))
    assert check_target(tmp_path, target) == ([], [])


@pytest.mark.parametrize(
    "separator", ["\f", "\v", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\r"]
)
def test_lines_are_counted_at_newlines_only(tmp_path: Path, separator: str) -> None:
    text = f"a = 1  # one{separator}still one\n\ndef f():\n    return 2\n"
    (tmp_path / "m.py").write_bytes(text.encode("utf-8"))
    (tmp_path / "m.ts").write_bytes(text.replace("def f():", "function f() {").encode("utf-8"))
    lines = read_lines(tmp_path, "m.py")
    assert len(lines) == 4
    assert lines[0] == f"a = 1  # one{separator}still one"
    target = GoldTarget(layer=Layer.HANDLER, path="m.py", symbol="f", lines=(3, 4))
    assert check_target(tmp_path, target) == ([], [])  # Python's spans count the same lines
    ts = GoldTarget(layer=Layer.UI, path="m.ts", lines=(4, 4))
    assert check_target(tmp_path, ts) == ([], [])
    past = GoldTarget(layer=Layer.UI, path="m.ts", lines=(5, 5))
    assert check_target(tmp_path, past) == (["m.ts: lines 5-5 run past the end (line 4)"], [])


def test_split_lines() -> None:
    assert split_lines("") == []
    assert split_lines("a\r\nb\n") == ["a", "b"]
    assert split_lines("a\n\n") == ["a", ""]
    assert split_lines("a\rb") == ["a\rb"]
    assert split_lines("no newline") == ["no newline"]


def test_gold_the_indexer_skips_is_an_error(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("generated.py\n", encoding="utf-8")
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "lib" / "index.js").write_text("x\n", encoding="utf-8")
    (tmp_path / "generated.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    indexed = indexed_paths(tmp_path)
    assert "real.py" in indexed
    for path in ("generated.py", "node_modules/lib/index.js", "link.py"):
        errors, _ = check_target(tmp_path, GoldTarget(layer=Layer.DB, path=path), indexed)
        assert len(errors) == 1
        assert errors[0].startswith(f"{path}: not a file the indexer keeps")
    real = GoldTarget(layer=Layer.DB, path="real.py")
    assert check_target(tmp_path, real, indexed) == ([], [])


def _item(n: int, cls: QueryClass, repo: str = "r") -> GoldenItem:
    answerable = cls is not QueryClass.UNANSWERABLE
    return GoldenItem(
        id=f"i{n}",
        repo=repo,
        sha=SHA,
        query=f"question number {n}",
        class_=cls,
        layers=[Layer.DB] if answerable else [],
        gold=[GoldTarget(layer=Layer.DB, path="a.py")] if answerable else [],
        answerable=answerable,
    )


def test_class_mix_departures_are_warnings() -> None:
    mix = (
        [QueryClass.CONCEPTUAL] * 10
        + [QueryClass.FLOW] * 10
        + [QueryClass.SYMBOL] * 5
        + [QueryClass.REGEX] * 3
        + [QueryClass.UNANSWERABLE] * 2
    )
    items = [_item(n, cls) for n, cls in enumerate(mix)]
    assert check_mix("s", Suite(name="s", version=0, items=items)) == []
    # Extra unanswerable items (langflow has three more) are fine.
    extra = [_item(100 + n, QueryClass.UNANSWERABLE) for n in range(3)]
    assert check_mix("s", Suite(name="s", version=0, items=items + extra)) == []
    off = [*items[1:-1], _item(99, QueryClass.IMPACT)]
    [problem] = check_mix("s", Suite(name="s", version=0, items=off))
    assert problem.warning
    assert problem.item == "repo r"
    assert problem.message == (
        "class mix departs from the plan: conceptual 9 (planned 10), impact 1 (planned 0), "
        "unanswerable 1 (planned at least 2)"
    )


def test_a_failed_fetch_is_reported_once(tmp_path: Path) -> None:
    def checkout_for(repo: str) -> Path | None:
        if repo == "a":
            msg = f"fetching {repo} failed: boom"
            raise CheckoutError(msg)
        return tmp_path

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    items = [_item(1, QueryClass.SYMBOL, "a"), _item(2, QueryClass.SYMBOL, "a")]
    items.append(_item(3, QueryClass.SYMBOL, "b").model_copy(update={"sha": SHA}))
    pinned = {
        "a": PinnedRepo(id="a", url="https://example.com/a", sha=SHA),
        "b": PinnedRepo(id="b", url="https://example.com/b", sha=SHA),
    }
    suite = Suite(name="s", version=0, items=items)
    problems = _errors(validate_suite("s", suite, pinned, checkout_for))
    # The other repository is still checked (its plain directory isn't a checkout at SHA).
    assert [(p.item, p.message) for p in problems] == [
        ("i1", "fetching a failed: boom"),
        ("i3", f"checkout {tmp_path} is not at {SHA}"),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "../escape"),
        ("id", "a..b"),
        ("id", "Upper"),
        ("id", "a/b"),
        ("url", "-uhttps://example.com/x"),
        ("url", "ext::sh -c touch% /tmp/pwned"),
        ("url", "git@github.com:a/b"),
        ("url", "http://example.com/x"),
    ],
)
def test_pinned_repos_are_validated(field: str, value: str) -> None:
    values = {"id": "ok-1.2_3", "url": "https://example.com/x", "sha": SHA, field: value}
    with pytest.raises(ValueError, match=field):
        PinnedRepo.model_validate(values)


def test_pinned_repo_url_schemes() -> None:
    for url in ("https://example.com/x", "ssh://git@example.com/x", "file:///tmp/x"):
        assert PinnedRepo(id="x", url=url, sha=SHA).url == url


def _git(directory: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def test_fetch_checkout_from_a_local_repository(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "a.py").write_text("x = 1\n", encoding="utf-8")
    (upstream / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(upstream, "init", "-q")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "one")
    sha = _git(upstream, "rev-parse", "HEAD")
    (upstream / "a.py").write_text("x = 3\n", encoding="utf-8")
    _git(upstream, "commit", "-q", "-am", "two")  # the pin is not the tip
    _git(upstream, "config", "uploadpack.allowAnySHA1InWant", "true")
    repo = PinnedRepo(id="up", url=upstream.as_uri(), sha=sha)
    directory = tmp_path / "cache" / "up"

    assert fetch_checkout(repo, directory) == directory
    assert _git(directory, "rev-parse", "HEAD") == sha
    assert (directory / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    # Local edits, untracked and ignored files are all gone after the next fetch.
    (directory / "a.py").write_text("x = 2\n", encoding="utf-8")
    (directory / "new.py").write_text("y = 1\n", encoding="utf-8")
    (directory / "debug.log").write_text("log\n", encoding="utf-8")
    fetch_checkout(repo, directory)
    assert (directory / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (directory / "new.py").exists()
    assert not (directory / "debug.log").exists()
    assert _git(directory, "status", "--porcelain", "--ignored") == ""


def test_fetch_checkout_failures_raise(tmp_path: Path) -> None:
    repo = PinnedRepo(id="gone", url=(tmp_path / "nowhere").as_uri(), sha=SHA)
    with pytest.raises(subprocess.CalledProcessError):
        fetch_checkout(repo, tmp_path / "cache" / "gone")


def test_load_renamed(tmp_path: Path) -> None:
    def mapping(repo_id: str, text: str, *, copy: bool = True) -> None:
        if copy:
            (tmp_path / repo_id).mkdir()
        mapping_path(tmp_path, repo_id).write_text(text, encoding="utf-8")

    mapping("copy", f'{{"repo": "copy", "sha": "{SHA}"}}')
    mapping("old", f'{{"sha": "{SHA}"}}')  # older mappings go by their file name
    mapping("moved", f'{{"repo": "copy", "sha": "{SHA}"}}')  # its id disagrees: pins nothing
    mapping("no-copy", f'{{"repo": "no-copy", "sha": "{SHA}"}}', copy=False)
    mapping("broken", "{")
    mapping("short", '{"repo": "short", "sha": "abc"}')
    found = load_renamed(tmp_path)
    assert sorted(found) == ["copy", "old"]
    assert found["copy"].sha == SHA
    assert found["copy"].url == (tmp_path / "copy").as_uri()


def test_load_renamed_relative_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "copies" / "copy").mkdir(parents=True)
    mapping_path(tmp_path / "copies", "copy").write_text(
        f'{{"repo": "copy", "sha": "{SHA}"}}', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    found = load_renamed(Path("copies"))
    assert found["copy"].url == (tmp_path / "copies" / "copy").resolve().as_uri()
