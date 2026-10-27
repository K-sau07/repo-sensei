"""The sandbox against a real Docker daemon: a run works, can't reach the network or change the
snapshot, is killed at its time limit and output cap, and leaves no container behind; then
dependency images. The runner image is built first when it's missing (`make sandbox-image`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sensei_engine.sandbox.deps import Dependencies, DependencyImages, detect
from sensei_engine.sandbox.runner import (
    LABEL,
    Limits,
    Sandbox,
    SandboxUnavailableError,
    docker_env,
    runner_image,
)

pytestmark = pytest.mark.integration

DOCKERFILE = Path(__file__).parents[3] / "sandbox" / "images" / "python-runner.Dockerfile"


def _docker(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a fixed argv, no shell
        ["docker", *args],  # noqa: S607 - docker from PATH, as the runner finds it
        capture_output=True,
        text=True,
        timeout=timeout,
        env=docker_env(),
        check=False,
    )


@pytest.fixture(scope="module")
def image() -> str:
    try:
        if _docker("info", "--format={{.ServerVersion}}").returncode != 0:
            pytest.skip("no docker daemon")
    except FileNotFoundError:
        pytest.skip("docker isn't installed")
    tag = runner_image()
    if _docker("image", "inspect", tag).returncode != 0:
        built = _docker(
            "build", f"--file={DOCKERFILE}", f"--tag={tag}", str(DOCKERFILE.parent), timeout=600
        )
        assert built.returncode == 0, built.stderr[-2000:]
    return tag


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "hello.py").write_text("import os\nprint('hello', os.getuid(), os.getcwd())\n")
    return root


def _leftovers() -> list[str]:
    listed = _docker("ps", "--all", "--quiet", f"--filter=label={LABEL}=run")
    return listed.stdout.split()


async def test_hello_world_runs_as_the_sandbox_user(image: str, snapshot: Path) -> None:
    result = await Sandbox().run(snapshot, ["python", "pkg/hello.py"])
    assert result.ok, result.stderr
    assert result.stdout == "hello 10001 /src\n"
    assert result.duration > 0
    in_package = await Sandbox().run(snapshot, ["python", "hello.py"], workdir="pkg")
    assert in_package.stdout == "hello 10001 /src/pkg\n"


async def test_the_network_is_blocked(image: str, snapshot: Path) -> None:
    script = (
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://1.1.1.1', timeout=3)\n"
        "except OSError as exc:\n"
        "    print('blocked', exc)\n"
        "else:\n"
        "    print('reached')\n"
        "print('routes', len(open('/proc/net/route').read().splitlines()) - 1)\n"
    )
    result = await Sandbox().run(snapshot, ["python", "-c", script])
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("blocked")
    assert "routes 0" in result.stdout  # nowhere to send a packet but loopback


async def test_the_snapshot_and_image_are_read_only_and_work_is_not(
    image: str, snapshot: Path
) -> None:
    sandbox = Sandbox()
    for target in ("/src/pkg/hello.py", "/src/new", "/opt/venv/x", "/etc/x"):
        result = await sandbox.run(snapshot, ["sh", "-c", f"echo x > {target}"])
        assert result.exit_code != 0, target
    assert (snapshot / "pkg" / "hello.py").read_text().startswith("import os")
    assert not (snapshot / "new").exists()
    result = await sandbox.run(snapshot, ["sh", "-c", "echo kept > /work/a && cat /work/a $HOME/a"])
    assert result.stdout == "kept\nkept\n"
    tmp = await sandbox.run(snapshot, ["sh", "-c", "echo t > /tmp/t && cat /tmp/t"])
    assert tmp.stdout == "t\n"


async def test_the_time_limit_kills_it(image: str, snapshot: Path) -> None:
    result = await Sandbox().run(snapshot, ["sleep", "60"], time_limit=1.5)
    assert result.timed_out
    assert result.exit_code is None
    assert not result.ok
    assert result.duration < 15
    unread = await Sandbox().run(snapshot, ["sleep", "60"], time_limit=1.5, stdin=b"x" * 2**23)
    assert unread.timed_out  # stdin nobody reads doesn't hold it past the limit
    assert unread.duration < 15
    assert _leftovers() == []


async def test_the_output_cap_is_enforced(image: str, snapshot: Path) -> None:
    sandbox = Sandbox(limits=Limits(max_output_bytes=1000))
    script = "import sys, time\nwhile True:\n    sys.stdout.write('x' * 4096)\n"
    result = await sandbox.run(snapshot, ["python", "-c", script], time_limit=30)
    assert result.truncated
    assert not result.timed_out  # stopped at the cap, long before the time limit
    assert result.exit_code is None
    assert len(result.stdout) == 1000
    assert _leftovers() == []


async def test_the_process_limit_holds(image: str, snapshot: Path) -> None:
    sandbox = Sandbox(limits=Limits(pids=16))
    spawn = (
        "import os, signal, time\n"
        "children = []\n"
        "for _ in range(40):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except OSError:\n"
        "        break\n"
        "    if pid == 0:\n"
        "        time.sleep(5)\n"
        "        os._exit(0)\n"
        "    children.append(pid)\n"
        "print(len(children))\n"
        "for pid in children:\n"
        "    os.kill(pid, signal.SIGKILL)\n"
    )
    result = await sandbox.run(snapshot, ["python", "-c", spawn], time_limit=30)
    assert 0 < int(result.stdout) < 16, result.stderr  # forty asked for, the rest refused
    files = await sandbox.run(snapshot, ["sh", "-c", "ulimit -n"])
    assert files.stdout.strip() == "1024"


async def test_exit_codes_stdin_and_redaction(image: str, snapshot: Path) -> None:
    sandbox = Sandbox()
    failed = await sandbox.run(snapshot, ["sh", "-c", "echo err >&2; exit 3"])
    assert (failed.exit_code, failed.stderr) == (3, "err\n")
    echoed = await sandbox.run(snapshot, ["cat"], stdin=b"from stdin")
    assert echoed.stdout == "from stdin"
    token = "ghp_" + "a" * 36
    leaked = await sandbox.run(snapshot, ["echo", f"token={token}"], env={"PLACEHOLDER": "x"})
    assert token not in leaked.stdout
    assert "***" in leaked.stdout
    env = await sandbox.run(snapshot, ["sh", "-c", "env | sort"], env={"PLACEHOLDER": "x"})
    names = {line.split("=", 1)[0] for line in env.stdout.splitlines()}
    assert "PLACEHOLDER" in names
    assert not {"DOCKER_HOST", "SENSEI_ENV", "ANTHROPIC_API_KEY"} & names
    assert _leftovers() == []


async def test_a_missing_image_is_unavailable(image: str, snapshot: Path) -> None:
    with pytest.raises(SandboxUnavailableError):
        await Sandbox().run(snapshot, ["true"], image="repo-sensei/no-such-image:0")


async def test_no_dependencies_use_the_runner_image(image: str, snapshot: Path) -> None:
    prepared = await DependencyImages(Sandbox()).prepare(detect(snapshot))
    assert (prepared.image, prepared.installed, prepared.limitation) == (image, True, None)


async def test_dependencies_that_dont_install_are_a_limitation(image: str, snapshot: Path) -> None:
    deps = Dependencies(project="", sources=(), python="3.13", requirements=("six==1.17.0",))
    images = DependencyImages(Sandbox(), index_url="http://127.0.0.1:9/simple", timeout=120)
    prepared = await images.prepare(deps)
    assert not prepared.installed
    assert prepared.image == image  # the run still gets the bare runner
    assert prepared.limitation is not None
    assert prepared.detail


@pytest.mark.slow
async def test_dependencies_install_once_into_an_image(image: str, snapshot: Path) -> None:
    """Needs pypi.org."""
    (snapshot / "requirements.txt").write_text("six==1.17.0\n")
    sandbox = Sandbox()
    images = DependencyImages(sandbox)
    deps = detect(snapshot)
    first = await images.prepare(deps)
    assert first.installed, first.detail
    assert first.image.startswith("repo-sensei/sandbox-deps:")
    again = await images.prepare(deps)
    assert (again.image, again.built) == (first.image, False)
    result = await sandbox.run(
        snapshot, ["python", "-c", "import six; print(six.__version__)"], image=first.image
    )
    assert result.stdout == "1.17.0\n"
