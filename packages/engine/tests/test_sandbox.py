"""The sandbox without Docker: the ``docker run`` it builds, what it refuses, and which
dependencies it reads from a repository's manifests."""

from __future__ import annotations

import asyncio
import os
import tarfile
import time
from pathlib import Path
from typing import Literal

import pytest

from sensei_engine.sandbox.deps import (
    Dependencies,
    DependencyImages,
    detect,
    dockerfile,
    install_output,
    requirement_lines,
)
from sensei_engine.sandbox.runner import (
    DEADLINE_LABEL,
    Limits,
    RunResult,
    Sandbox,
    SandboxError,
    SandboxUnavailableError,
    container_workdir,
    expired,
    mount_source,
    run_argv,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"


def _argv(tmp_path: Path, **overrides: object) -> list[str]:
    args: dict[str, object] = {
        "binary": "docker",
        "name": "sensei-sandbox-x",
        "image": "repo-sensei/python-runner:3.13",
        "source": tmp_path,
        "workdir": "/src",
        "cmd": ["python", "-c", "print(1)"],
        "env": {},
        "limits": Limits(),
        "runtime": None,
        "stdin": False,
        "time_limit": 120.0,
        "now": 1_800_000_000.0,
    }
    args.update(overrides)
    return run_argv(**args)  # type: ignore[arg-type]


def test_a_run_has_no_network_a_read_only_snapshot_and_limits(tmp_path: Path) -> None:
    argv = _argv(tmp_path, limits=Limits(cpus=1.5, memory_mb=512, pids=64))
    assert argv[:3] == ["docker", "run", "--rm"]
    for flag in (
        "--network=none",
        "--read-only",
        f"--mount=type=bind,source={tmp_path},target=/src,readonly",
        "--user=10001:10001",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pull=never",
        "--log-driver=none",
        "--cpus=1.5",
        "--memory=512m",
        "--memory-swap=512m",
        "--pids-limit=64",
        "--env=HOME=/work",
    ):
        assert flag in argv
    assert any(arg.startswith("--tmpfs=/work:") for arg in argv)
    assert not any(arg.startswith("--runtime") for arg in argv)
    # The command comes after the image: none of it is read as docker's own flags. It runs
    # under the container's own limit, and the container says when it must be gone.
    image = argv.index("repo-sensei/python-runner:3.13")
    assert argv[image + 1 :] == [
        "/usr/bin/timeout",
        "--signal=KILL",
        "125",
        "python",
        "-c",
        "print(1)",
    ]
    assert "--label=dev.repo-sensei.sandbox.deadline=1800000155" in argv


def test_gvisor_stdin_and_env_are_passed_when_asked(tmp_path: Path) -> None:
    argv = _argv(tmp_path, runtime="runsc", stdin=True, env={"B": "2", "A": "x=y"})
    assert "--runtime=runsc" in argv
    assert "--interactive" in argv
    assert argv.index("--env=A=x=y") < argv.index("--env=B=2")


def test_a_command_starting_with_a_dash_stays_the_commands(tmp_path: Path) -> None:
    argv = _argv(tmp_path, cmd=["--privileged"])
    assert argv[-5:] == [
        "repo-sensei/python-runner:3.13",
        "/usr/bin/timeout",
        "--signal=KILL",
        "125",
        "--privileged",
    ]


@pytest.mark.parametrize(
    "env",
    [{"A B": "1"}, {"1A": "1"}, {"": "1"}, {"A": "nul\0"}, {f"V{i}": "1" for i in range(65)}],
)
def test_bad_environment_variables_are_refused(tmp_path: Path, env: dict[str, str]) -> None:
    with pytest.raises(SandboxError):
        _argv(tmp_path, env=env)


@pytest.mark.parametrize("cmd", [[], [""], ["x\0"], ["a"] * 257])
def test_bad_commands_are_refused(tmp_path: Path, cmd: list[str]) -> None:
    with pytest.raises(SandboxError):
        _argv(tmp_path, cmd=cmd)


def test_the_working_directory_stays_in_the_snapshot() -> None:
    assert container_workdir("") == "/src"
    assert container_workdir("backend/") == "/src/backend"
    for bad in ("../etc", "/etc", "a/../../b", "a\\b"):
        with pytest.raises(SandboxError):
            container_workdir(bad)


def test_only_a_real_directory_is_mounted(tmp_path: Path) -> None:
    real = tmp_path / "snapshot"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert mount_source(real) == real.resolve()
    with pytest.raises(SandboxError):
        mount_source(link)
    with pytest.raises(SandboxError):
        mount_source(tmp_path / "missing")


def test_only_our_containers_past_their_deadline_are_swept() -> None:
    listing = (
        "sensei-sandbox-old 1799999999\n"
        "sensei-sandbox-live 1800000100\n"
        "sensei-sandbox-nolabel \n"
        "someone-else 1700000000\n"
        "sensei-sandbox-odd 17x\n"
        "sensei-sandbox-superscript \u00b9\n"
        "sensei-sandbox-arabic \u0661\u0667\n"
    )
    assert expired(listing, 1_800_000_000.0) == ["sensei-sandbox-old"]


async def test_a_queued_runs_deadline_counts_from_when_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1_800_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    started: list[list[str]] = []
    release = asyncio.Event()

    class Recorded(Sandbox):
        def docker(self) -> str:
            return "docker"

        async def sweep(self) -> int:
            return 0

        async def _run(
            self, argv: list[str], name: str, time_limit: float, stdin: bytes | None
        ) -> RunResult:
            started.append(argv)
            await release.wait()
            return RunResult(0, "", "", 0.0)

    both_asked = asyncio.Event()

    class Counted(asyncio.Semaphore):
        asked = 0

        async def acquire(self) -> Literal[True]:
            self.asked += 1
            if self.asked == 2:
                both_asked.set()
            return await super().acquire()

    sandbox = Recorded()
    sandbox._slots = Counted(1)  # pyright: ignore[reportPrivateUsage]
    first = asyncio.create_task(sandbox.run(tmp_path, ["true"], time_limit=60))
    queued = asyncio.create_task(sandbox.run(tmp_path, ["true"], time_limit=60))
    await both_asked.wait()  # the first is running (it had a free slot), the second waiting
    assert len(started) == 1
    clock[0] += 600  # the first run takes ten minutes; the second waits for its slot
    release.set()
    await asyncio.gather(first, queued)

    def listing(argv: list[str]) -> str:
        name = next(arg for arg in argv if arg.startswith("--name="))
        label = next(arg for arg in argv if arg.startswith(f"--label={DEADLINE_LABEL}="))
        return f"{name.removeprefix('--name=')} {label.rpartition('=')[2]}"

    assert len(started) == 2
    # A sweep (another run's) a minute into the second run leaves it alone.
    assert expired(listing(started[1]), clock[0] + 60) == []
    assert expired(listing(started[0]), clock[0] + 60) != []


def test_a_run_is_ok_only_when_it_finished_with_zero() -> None:
    assert RunResult(0, "", "", 0.1).ok
    assert not RunResult(1, "", "", 0.1).ok
    assert not RunResult(None, "", "", 0.1, timed_out=True).ok
    assert not RunResult(None, "", "", 0.1, truncated=True).ok


async def test_a_missing_docker_is_unavailable(tmp_path: Path) -> None:
    sandbox = Sandbox(binary="no-such-docker-binary")
    with pytest.raises(SandboxUnavailableError):
        await sandbox.run(tmp_path, ["true"])
    assert await sandbox.image_id("anything") is None


# ---------------------------------------------------------------- dependencies


def _write(root: Path, files: dict[str, str]) -> Path:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    return root


def test_requirements_files_keep_only_plain_requirements(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "requirements.txt": (
                "# pinned\n"
                "fastapi==0.115.0  # the web framework\n"
                "uvicorn[standard]>=0.30 \\\n    ; python_version >= '3.10'\n"
                "httpx==0.28.1 --hash=sha256:abc\n"
                "-r other.txt\n"
                "--index-url https://evil.example/simple\n"
                "-e .\n"
                "mylib @ git+https://github.com/x/mylib\n"
                "./local-package\n"
                "\n"
            ),
            "requirements-dev.txt": "pytest>=8\n",
            "requirements.in": "never-read\n",
        },
    )
    deps = detect(root)
    assert deps.sources == ("requirements-dev.txt", "requirements.txt")
    assert deps.requirements == (
        "fastapi==0.115.0",
        "httpx==0.28.1",
        "pytest>=8",
        'uvicorn[standard]>=0.30; python_version >= "3.10"',
    )
    skipped = "\n".join(deps.skipped)
    for reason in ("-r other.txt", "--index-url", "-e .", "mylib comes from a URL", "./local"):
        assert reason in skipped
    assert deps.constraints == ()


def test_requirement_lines_join_continuations_and_drop_comments() -> None:
    text = "a==1 \\\n  --hash=sha256:x \\\n  --hash=sha256:y\n#c\n b>=2 # note\n"
    assert requirement_lines(text) == ["a==1", "b>=2"]


def test_pyproject_dependencies_and_python_version(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "svc/pyproject.toml": (
                '[project]\nname = "svc"\nrequires-python = ">=3.10,<3.13"\n'
                'dependencies = ["fastapi>=0.110", "pkg @ https://x.example/pkg.whl"]\n'
            )
        },
    )
    deps = detect(root, "svc")
    assert deps.project == "svc"
    assert deps.sources == ("svc/pyproject.toml",)
    assert deps.python == "3.12"  # 3.13 is the default, but the range stops short of it
    assert deps.requirements == ("fastapi>=0.110",)
    assert any("pkg comes from a URL" in reason for reason in deps.skipped)


def test_a_python_range_no_runner_fits_is_noted(tmp_path: Path) -> None:
    root = _write(tmp_path, {"pyproject.toml": '[project]\nrequires-python = "<3.8"\n'})
    deps = detect(root)
    assert deps.python == "3.13"
    assert any("allows none of" in reason for reason in deps.skipped)
    assert deps.empty


LOCK = """
version = 1
requires-python = ">=3.14"

[[package]]
name = "app"
version = "0.1.0"
source = { editable = "backend" }
[package.metadata]
requires-dist = [
    { name = "fastapi", extras = ["standard"], specifier = ">=0.1" },
    { name = "core", editable = "libs/core" },
    { name = "tool", git = "https://github.com/x/tool" },
    { name = "pytest", marker = "extra == 'test'" },
]

[[package]]
name = "core"
version = "0.1.0"
source = { editable = "libs/core" }
[package.metadata]
requires-dist = [
    { name = "pydantic", specifier = ">=2" },
    { name = "orjson", marker = "extra == 'fast'" },
    { name = "colorama", marker = "sys_platform == 'win32'" },
]

[[package]]
name = "fastapi"
version = "0.115.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pydantic"
version = "2.9.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "numpy"
version = "1.26.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "numpy"
version = "2.1.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "tool"
version = "0.1.0"
source = { git = "https://github.com/x/tool" }
"""


def test_a_workspace_lock_is_walked_from_the_project(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "pyproject.toml": '[tool.uv.workspace]\nmembers = ["backend", "libs/*"]\n',
            "uv.lock": LOCK,
            "backend/pyproject.toml": '[project]\nname = "app"\n',
            "backend/requirements.txt": "ignored-when-locked\n",
        },
    )
    deps = detect(root, "backend")
    assert deps.sources == ("uv.lock",)  # the workspace's, above the project
    assert deps.python == "3.14"  # from the lock's range
    assert deps.requirements == (
        'colorama; sys_platform == "win32"',
        "fastapi[standard]>=0.1",
        "pydantic>=2",
    )  # core's requirements through the member; the extras nobody asked for are left out
    assert deps.constraints == ("fastapi==0.115.0", "pydantic==2.9.0")  # numpy has two
    assert any("tool isn't from the index" in reason for reason in deps.skipped)


INJECTIONS = ("\n--extra-index-url https://evil/simple", "x @ https://evil/x.tar.gz")


@pytest.mark.parametrize("injection", INJECTIONS)
def test_a_lock_cannot_put_options_or_urls_in_the_install(tmp_path: Path, injection: str) -> None:
    """The lock is the repository's: a pin's name or version, or a requirement's specifier,
    that would add an index or a URL to the build (which has the network, as root) is dropped."""
    quoted = injection.replace("\n", "\\n")
    lock = f"""
version = 1

[[package]]
name = "app"
version = "0.1.0"
source = {{ editable = "." }}
[package.metadata]
requires-dist = [
    {{ name = "fastapi", specifier = ">=0.1" }},
    {{ name = "evil-spec", specifier = "{quoted}" }},
]

[[package]]
name = "fastapi"
version = "0.115.0"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "evil-version"
version = "1.0{quoted}"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "evil{quoted}"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
"""
    root = _write(tmp_path, {"pyproject.toml": '[project]\nname = "app"\n', "uv.lock": lock})
    deps = detect(root)
    assert deps.constraints == ("fastapi==0.115.0",)
    assert deps.requirements == ("fastapi>=0.1",)
    for line in (*deps.constraints, *deps.requirements):
        assert "\n" not in line
        assert "evil" not in line
    assert sum("not a pin" in reason for reason in deps.skipped) == 2
    assert any("evil-spec" in reason for reason in deps.skipped)  # not one, or from a URL


def test_a_pins_name_is_all_name(tmp_path: Path) -> None:
    lock = """
version = 1

[[package]]
name = "app"
version = "0.1.0"
source = { editable = "." }

[[package]]
name = "trailing\\n"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
"""
    root = _write(tmp_path, {"pyproject.toml": '[project]\nname = "app"\n', "uv.lock": lock})
    deps = detect(root)
    assert deps.constraints == ()
    assert any("not a pin" in reason for reason in deps.skipped)


def test_a_symlinked_manifest_is_not_read(tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside", {"requirements.txt": "secret-package\n"})
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "requirements.txt").symlink_to(outside / "requirements.txt")
    (root / "pyproject.toml").symlink_to(outside / "requirements.txt")
    assert detect(root).empty
    with pytest.raises(SandboxError):
        detect(root, "../outside")


def test_the_fixture_backend_is_found_through_the_root_lock(tmp_path: Path) -> None:
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(tmp_path, filter="data")
    deps = detect(tmp_path / "fastapi-template", "backend")
    assert deps.sources == ("uv.lock",)
    assert deps.python == "3.14"
    names = {line.split("[")[0].split("<")[0].split(">")[0] for line in deps.requirements}
    assert {"fastapi", "sqlmodel", "alembic", "pyjwt", "psycopg"} <= names
    assert len(deps.constraints) > 50
    assert deps.skipped == ()


def test_the_image_key_changes_with_what_it_installs() -> None:
    deps = Dependencies(project="", sources=(), python="3.13", requirements=("a==1",))
    other = Dependencies(project="x", sources=("y",), python="3.13", requirements=("a==1",))
    assert deps.key("sha256:1", "https://pypi.org/simple") == other.key(
        "sha256:1", "https://pypi.org/simple"
    )
    assert deps.key("sha256:1", "https://pypi.org/simple") != deps.key(
        "sha256:2", "https://pypi.org/simple"
    )
    newer = Dependencies(project="", sources=(), python="3.13", requirements=("a==2",))
    assert deps.key("i", "u") != newer.key("i", "u")


def test_the_install_is_wheels_only_unless_source_builds() -> None:
    wheels = dockerfile("base:1", "https://pypi.org/simple", constraints=True, source_builds=False)
    assert "--no-build" in wheels
    assert "--no-config" in wheels
    assert "--index-url https://pypi.org/simple" in wheels
    assert "--constraint /tmp/sensei/constraints.txt" in wheels
    assert wheels.startswith("FROM base:1\n")
    assert "USER 10001:10001" in wheels
    assert "COPY" not in wheels  # mounted for the install only, never a layer
    sources = dockerfile("base:1", "https://pypi.org/simple", constraints=False, source_builds=True)
    assert "--no-build" not in sources
    assert "--constraint" not in sources


@pytest.mark.parametrize("url", ["file:///etc", "https://x y", "https://x\\nRUN", "ftp://x"])
def test_an_index_url_must_be_plain_http(url: str) -> None:
    with pytest.raises(SandboxError):
        DependencyImages(Sandbox(), index_url=url)


def test_the_install_output_drops_dockers_prefixes_and_secrets() -> None:
    out = (
        b"#1 [internal] load build definition\n"
        b"#7 0.710 error: No solution found\n"
        b"#7 0.711 token ghp_abcdefghijklmnopqrstuvwxyz0123\n"
        b"#7 DONE 0.8s\n"
    )
    text = install_output(out)
    assert text.splitlines() == ["error: No solution found", "token ***"]


def test_detect_only_reads(tmp_path: Path) -> None:
    root = _write(tmp_path, {"requirements.txt": "a==1\n"})
    before = sorted(os.listdir(root))
    detect(root)
    assert sorted(os.listdir(root)) == before
