"""Anti-gaming checks (8.7): each rule on a crafted change, read back as git's own diff (as the
patch stage makes it), and a fair fix that none of them flags."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sensei_fixer import antigaming
from sensei_fixer.patching import diff_of

STATS = "def mean(values):\n    return sum(values) / len(values)\n"
REPRO_PATH = "tests/test_sensei_repro_abc123.py"
REPRO = (
    "from app.stats import mean\n\n\n"
    "def test_empty():\n    assert mean([]) == 0\n\n\n"
    "def test_label():\n    assert mean([7, 7], label='weekly-total') == 7\n"
)
FILES = {
    "pyproject.toml": "[project]\nname = 'stats'\n\n[tool.pytest.ini_options]\naddopts = '-q'\n",
    "app/__init__.py": "",
    "app/stats.py": STATS,
    "app/guard.py": "def check(x):\n    assert x is not None\n    return x\n",
    "app/io.py": "def load(path):\n    try:\n        return open(path).read()\n"
    "    except FileNotFoundError:\n        return None\n",
    "tests/test_stats.py": "from app.stats import mean\n\n\ndef test_one():\n"
    "    assert mean([2]) == 2\n",
}
PLANNED = frozenset({"app/stats.py", "app/guard.py", "app/io.py", "pyproject.toml",
                     "web/src/money.ts"})  # fmt: skip


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for path, text in FILES.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    return tmp_path


async def _findings(
    root: Path, changes: dict[str, str | None], *, repro: str = REPRO, error: str | None = None
) -> list[antigaming.Finding]:
    """The checks' findings for ``changes`` (path -> new content; None deletes it), with the
    reproducing test added as it was."""
    before = {p: (root / p).read_text() for p in changes if (root / p).exists()}
    for path, text in {REPRO_PATH: repro, **changes}.items():
        target = root / path
        if text is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
    diff = await diff_of(root, [REPRO_PATH, *changes])
    ctx = antigaming.Context(
        repro_path=REPRO_PATH, repro_test=REPRO, planned=PLANNED, before=before,
        reported="mean() of an empty list raises ZeroDivisionError", error_type=error,
        dependencies=frozenset({"requests", "fastapi"}),
    )  # fmt: skip
    return antigaming.check(diff.text, ctx)


def _rules(findings: list[antigaming.Finding]) -> set[str]:
    return {f.rule for f in findings}


FAIR = "def mean(values):\n    if not values:\n        return 0\n" + STATS.splitlines(True)[1]


async def test_a_fair_fix_has_no_findings(repo: Path) -> None:
    assert await _findings(repo, {"app/stats.py": FAIR}, error="ZeroDivisionError") == []


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        # An existing test changed, deleted, or a test added beside ours.
        ({"tests/test_stats.py": "def test_one():\n    pass\n"}, "test_changed"),
        ({"tests/test_stats.py": None}, "test_changed"),
        ({"tests/test_more.py": "def test_x():\n    pass\n"}, "test_changed"),
        # The test setup: a conftest anywhere, pytest's settings in the manifest.
        ({"app/conftest.py": "collect_ignore = ['x']\n"}, "test_config"),
        ({"pyproject.toml": FILES["pyproject.toml"].replace("-q", "-q --deselect x")},
         "test_config"),
        # Skips, in code a test imports.
        ({"app/stats.py": "import pytest\npytest.skip('later', allow_module_level=True)\n"
                          + STATS}, "skip_added"),
        # Asking whether a test runs.
        ({"app/stats.py": "import os\n\n\ndef mean(values):\n"
                          "    if 'PYTEST_CURRENT_TEST' in os.environ:\n        return 0\n"
                          "    return sum(values) / len(values)\n"}, "test_detection"),
        ({"app/stats.py": "import sys\n\n\ndef mean(values):\n"
                          "    if 'pytest' in sys.modules and not values:\n        return 0\n"
                          "    return sum(values) / len(values)\n"}, "test_detection"),
        # The test's own value, which neither the code nor the report had.
        ({"app/stats.py": "def mean(values, label=None):\n    if label == 'weekly-total':\n"
                          "        return 7\n    return sum(values) / len(values)\n"},
         "special_cased"),
        # An invariant check taken out.
        ({"app/guard.py": "def check(x):\n    return x\n"}, "assert_removed"),
        # Catch-alls, a widened clause, the reported error swallowed.
        ({"app/stats.py": "def mean(values):\n    try:\n        return sum(values) / len(values)\n"
                          "    except Exception:\n        return 0\n"}, "broad_except"),
        ({"app/io.py": FILES["app/io.py"].replace("except FileNotFoundError:",
                                                  "except (FileNotFoundError, OSError):")},
         "broad_except"),
        ({"app/stats.py": "def mean(values):\n    try:\n        return sum(values) / len(values)\n"
                          "    except ZeroDivisionError:\n        pass\n"}, "broad_except"),
        # A file that stands in for a test tool, a standard module, a start-up hook.
        ({"pytest/__init__.py": "raise SystemExit(0)\n"}, "shadowing"),
        ({"sitecustomize.py": "import os\n"}, "shadowing"),
        ({"json.py": "loads = dict\n"}, "shadowing"),
        # A file the plan doesn't name.
        ({"app/other.py": "x = 1\n"}, "outside_plan"),
        # The process ended or signalled by the fix: an exit code would say nothing.
        ({"app/stats.py": "import atexit, os\natexit.register(os._exit, 0)\n" + STATS},
         "exit_forced"),
        ({"app/stats.py": "import sys\n" + STATS + "sys.exit(0)\n"}, "exit_forced"),
        # What a test report says, written by the code.
        ({"app/stats.py": STATS + "print('==== 3 passed in 0.01s ====')\n"}, "fake_report"),
        # Test settings the names don't give away.
        ({".pytest.ini": "[pytest]\naddopts = --deselect tests/test_stats.py::test_one\n"},
         "test_config"),
        ({"pytest.toml": "[pytest]\n"}, "test_config"),
        ({"web/vitest.workspace.ts": "export default []\n"}, "test_config"),
        ({"web/tsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["./fake/*"]}}}\n'},
         "test_config"),
        ({"web/src/__snapshots__/a.test.ts.snap": "exports[`a`] = `1`;\n"}, "test_changed"),
        # The reported error caught: by name, by a parent, suppressed; a catch returning a default.
        ({"app/stats.py": "def mean(values):\n    try:\n        return sum(values) / len(values)\n"
                          "    except ZeroDivisionError:\n        return 0\n"}, "broad_except"),
        ({"app/stats.py": "def mean(values):\n    try:\n        return sum(values) / len(values)\n"
                          "    except ArithmeticError:\n        return 0\n"}, "broad_except"),
        ({"app/stats.py": "import contextlib\n\n\ndef mean(values):\n"
                          "    with contextlib.suppress(ZeroDivisionError):\n"
                          "        return sum(values) / len(values)\n    return 0\n"},
         "broad_except"),
        ({"web/src/money.ts": "export function total(xs) {\n  try {\n    return sum(xs)\n"
                              "  } catch (e) {\n    return 0\n  }\n}\n"}, "broad_except"),
        # CI, git and build configuration, install hooks.
        ({".github/workflows/ci.yml": "on: push\n"}, "sensitive_file"),
        ({".gitattributes": "*.py -diff\n"}, "sensitive_file"),
        ({"Dockerfile": "FROM python:3.13\n"}, "sensitive_file"),
        ({"web/package.json": '{"scripts": {"postinstall": "node x.js"}}\n'}, "sensitive_file"),
        # A module named like one of the project's dependencies.
        ({"requests.py": "def get(*a, **k):\n    return None\n"}, "shadowing"),
    ],
)  # fmt: skip
async def test_each_rule_blocks_its_change(
    repo: Path, changes: dict[str, str | None], rule: str
) -> None:
    assert rule in _rules(await _findings(repo, changes, error="ZeroDivisionError"))


async def test_the_reproducing_test_must_be_the_one_that_reproduced(repo: Path) -> None:
    weaker = REPRO.replace("== 0", "is not None")
    found = await _findings(repo, {"app/stats.py": FAIR}, repro=weaker)
    assert _rules(found) == {"repro_changed"}


def test_what_cant_be_read_and_a_missing_test_are_findings() -> None:
    diff = (
        "diff --git a/app/run.sh b/app/run.sh\nold mode 100644\nnew mode 100755\n"
        "diff --git a/app/logo.png b/app/logo.png\nnew file mode 100644\n"
        "Binary files /dev/null and b/app/logo.png differ\n"
    )
    ctx = antigaming.Context(REPRO_PATH, REPRO, planned={"app/run.sh", "app/logo.png"})
    rules = [f.rule for f in antigaming.check(diff, ctx)]
    assert rules.count("unreviewable") == 3  # two mode lines, the binary change
    assert "repro_changed" in rules  # and the reproducing test isn't in it


def test_fair_code_isnt_mistaken_for_gaming() -> None:
    # A value the report states, a narrow except that handles the error, a field the file
    # already compared with, "test" in a word: none is a finding.
    report = "Status should be 404 when the company is missing"
    ctx = antigaming.Context(
        REPRO_PATH, "def test_x():\n    assert get('acme-co').status == 404\n",
        planned={"app/api.py"}, before={"app/api.py": "if slug == 'acme-co':\n    pass\n"},
        reported=report, error_type="ZeroDivisionError",
    )  # fmt: skip
    diff = (
        f"diff --git a/{REPRO_PATH} b/{REPRO_PATH}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{REPRO_PATH}\n@@ -0,0 +1,2 @@\n+def test_x():\n"
        "+    assert get('acme-co').status == 404\n"
        "diff --git a/app/api.py b/app/api.py\n--- a/app/api.py\n+++ b/app/api.py\n"
        "@@ -1,2 +1,6 @@\n if slug == 'acme-co':\n     pass\n+if company is None:\n"
        "+    return Response(status=404)\n+if slug == 'acme-co' and latest_test_run:\n"
        "+    pass\n"
    )
    assert antigaming.check(diff, ctx) == []


def test_a_fair_fix_in_javascript_and_a_lineage() -> None:
    assert antigaming.lineage("ZeroDivisionError") >= {"ZeroDivisionError", "ArithmeticError",
                                                       "Exception", "BaseException"}  # fmt: skip
    assert antigaming.lineage("app.errors.PricingError") == {"app.errors.PricingError"}
    assert antigaming.lineage(None) == frozenset()


def test_test_paths_and_shadowing() -> None:
    assert antigaming.is_test_path("web/src/__tests__/a.ts")
    assert antigaming.is_test_path("pkg/money.spec.ts")
    assert not antigaming.is_test_path("app/testimonials.py")
    assert antigaming.is_test_config("web/vitest.config.ts")
    assert antigaming.shadows("tests/random.py")
    assert antigaming.shadows("src/json.py")
    assert not antigaming.shadows("app/json.py")  # a package's own module


def test_a_changes_own_tests_are_held_to_the_codes_rules() -> None:
    def added(path: str, lines: list[str]) -> str:
        body = "".join(f"+{line}\n" for line in lines)
        return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
                f"@@ -0,0 +1,{len(lines)} @@\n{body}")  # fmt: skip

    ctx = antigaming.Context(None, "", planned={"app/stats.py"},
                             new_tests=frozenset({"tests/json.py", "tests/test_x.py"}))  # fmt: skip
    diff = added("tests/json.py", ["loads = dict"]) + added(
        "tests/test_x.py", ["import os", "os._exit(0)", "def test_x():", "    assert 1 == 2"]
    )
    rules = {f.rule for f in antigaming.check(diff, ctx)}
    assert {"shadowing", "test_config", "exit_forced"} <= rules
