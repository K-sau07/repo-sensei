"""The sandbox against a real Docker daemon: a run works, can't reach the network or change the
snapshot, is killed at its time limit and output cap, and leaves no container behind; then
dependency images. The runner image is built first when it's missing (`make sandbox-image`)."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from sensei_engine.sandbox.deps import Dependencies, DependencyImages, detect
from sensei_engine.sandbox.network import BUILD_NETWORK, PROXY_IMAGE, PROXY_URL, Networks
from sensei_engine.sandbox.node import NODE_IMAGE, NodeImages, detect_node, node_command
from sensei_engine.sandbox.runner import (
    LABEL,
    Limits,
    Sandbox,
    SandboxUnavailableError,
    docker_env,
    runner_image,
)
from sensei_engine.sandbox.sidecars import POSTGRES_IMAGE, postgres

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


IMAGES = DOCKERFILE.parent
PROXY_SOURCE = Path(__file__).parents[1] / "src" / "sensei_engine" / "sandbox"


def _build(dockerfile: str, tag: str, context: Path) -> None:
    if _docker("image", "inspect", tag).returncode == 0:
        return
    built = _docker(
        "build", f"--file={IMAGES / dockerfile}", f"--tag={tag}", str(context), timeout=900
    )
    assert built.returncode == 0, built.stderr[-2000:]


@pytest.fixture(scope="module")
def proxy_image(image: str) -> str:
    """The registry proxy's image (installs go through it), built when it's missing."""
    _build("registry-proxy.Dockerfile", PROXY_IMAGE, PROXY_SOURCE)
    return PROXY_IMAGE


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


async def test_dependencies_that_dont_install_are_a_limitation(
    image: str, proxy_image: str, snapshot: Path
) -> None:
    deps = Dependencies(project="", sources=(), python="3.13", requirements=("six==1.17.0",))
    images = DependencyImages(Sandbox(), index_url="http://127.0.0.1:9/simple", timeout=120)
    prepared = await images.prepare(deps)
    assert not prepared.installed
    assert prepared.image == image  # the run still gets the bare runner
    assert prepared.limitation is not None
    assert prepared.detail


@pytest.mark.slow
async def test_dependencies_install_once_into_an_image(
    image: str, proxy_image: str, snapshot: Path
) -> None:
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
    # Installed by the installer (never root), and the image runs the base's command, not the
    # install's.
    owner = await sandbox.run(
        snapshot, ["python", "-c", "import os, six; print(os.stat(six.__file__).st_uid)"],
        image=first.image,
    )  # fmt: skip
    assert owner.stdout == "10002\n"
    config = _docker("image", "inspect", "--format={{json .Config.Cmd}}", first.image)
    assert config.stdout.strip() == '["python3"]'


# ---------------------------------------------------------------- 8.2 hardening


async def test_the_host_environment_mounts_and_docker_dont_reach_it(
    image: str, snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENSEI_HOST_SECRET", "do-not-leak")
    script = (
        "import os\n"
        "print('secret', 'SENSEI_HOST_SECRET' in os.environ)\n"
        "print('docker', os.path.exists('/var/run/docker.sock'))\n"
        "targets = [l.split()[4] for l in open('/proc/self/mountinfo')]\n"
        "binds = [t for t in targets if t.startswith(('/Users', '/home', '/root', '/var/run'))]\n"
        "print('host', binds)\n"
        "print('src', '/src' in targets)\n"
    )
    result = await Sandbox().run(snapshot, ["python", "-c", script])
    assert result.ok, result.stderr
    assert result.stdout.splitlines() == ["secret False", "docker False", "host []", "src True"]


async def test_a_memory_bomb_is_killed(image: str, snapshot: Path) -> None:
    bomb = "x = bytearray(1024 * 1024 * 1024)\nprint('allocated')\n"
    result = await Sandbox(limits=Limits(memory_mb=256)).run(snapshot, ["python", "-c", bomb])
    assert not result.ok
    assert "allocated" not in result.stdout
    assert _leftovers() == []


def _from_build_network(
    image: str, script: str, *, proxy: bool
) -> subprocess.CompletedProcess[str]:
    env = [f"--env=HTTPS_PROXY={PROXY_URL}"] if proxy else []
    return _docker(
        "run", "--rm", f"--network={BUILD_NETWORK}", *env, image, "python", "-c", script,
        timeout=120,
    )  # fmt: skip


FETCH = (
    "import sys, urllib.request\n"
    "for url in sys.argv[1:] or ['https://pypi.org/simple/six/', 'https://example.com/']:\n"
    "    try:\n"
    "        urllib.request.urlopen(url, timeout=20)\n"
    "        print(url, 'ok')\n"
    "    except Exception as exc:\n"
    "        print(url, 'refused', type(exc).__name__)\n"
)


@pytest.mark.slow
async def test_installs_reach_the_registries_and_nothing_else(image: str, proxy_image: str) -> None:
    """Needs pypi.org."""
    await Networks(Sandbox()).proxy()
    through = _from_build_network(image, FETCH, proxy=True)
    assert through.returncode == 0, through.stderr
    assert through.stdout.splitlines() == [
        "https://pypi.org/simple/six/ ok",
        "https://example.com/ refused URLError",
    ]
    direct = _from_build_network(image, FETCH, proxy=False)
    assert "https://pypi.org/simple/six/ refused" in direct.stdout  # no way out but the proxy


async def test_a_database_sidecar_is_there_for_one_run_only(image: str, snapshot: Path) -> None:
    if _docker("image", "inspect", POSTGRES_IMAGE).returncode != 0:
        pytest.skip(f"no {POSTGRES_IMAGE} (make sandbox-image)")
    sandbox = Sandbox()
    networks = Networks(sandbox)
    script = (
        "import os, socket, urllib.request\n"
        "socket.create_connection(('db', 5432), timeout=5).close()\n"
        "print('db', os.environ['DATABASE_URL'])\n"
        "try:\n"
        "    urllib.request.urlopen('http://1.1.1.1', timeout=3)\n"
        "    print('out')\n"
        "except OSError:\n"
        "    print('no way out')\n"
    )
    async with postgres(sandbox, networks) as services:
        result = await sandbox.run(
            snapshot, ["python", "-c", script], network=services.network, env=services.env
        )
        network = services.network
    assert result.ok, result.stderr
    assert result.stdout.splitlines() == [
        "db postgresql://sandbox:***@db:5432/app",  # what a run prints is redacted, as always
        "no way out",
    ]
    assert _docker("network", "inspect", network).returncode != 0  # gone with the run
    sidecars = _docker("ps", "--all", "--quiet", f"--filter=label={LABEL}=sidecar")
    assert sidecars.stdout.split() == []


@pytest.mark.slow
async def test_node_dependencies_install_behind_the_proxy_and_runs_use_them(
    proxy_image: str, snapshot: Path
) -> None:
    """Needs registry.npmjs.org (and npm here, to lock the test project)."""
    _build("node-runner.Dockerfile", NODE_IMAGE, IMAGES)
    web = snapshot / "web"
    web.mkdir()
    (web / "package.json").write_text(
        '{"name": "web", "private": true, "dependencies": {"is-number": "7.0.0"}}'
    )
    locked = await asyncio.to_thread(
        subprocess.run,
        ["npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit"],
        cwd=web, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    if locked.returncode != 0:
        pytest.skip(f"couldn't lock the test project: {locked.stderr[-200:]}")
    sandbox = Sandbox()
    prepared = await NodeImages(sandbox).prepare(detect_node(snapshot, "web"))
    assert prepared.installed, prepared.detail
    assert prepared.image.startswith("repo-sensei/sandbox-node-deps:")
    script = (
        "const isNumber = require('is-number');\n"
        "require('fs').writeFileSync('written.txt', 'ok');\n"  # the copy is writable
        "console.log(isNumber(5), process.cwd());\n"
    )
    result = await sandbox.run(
        snapshot, node_command("web", ["node", "-e", script]), image=prepared.image
    )
    assert result.ok, result.stderr
    assert result.stdout == "true /work/src/web\n"
    assert not (web / "written.txt").exists()  # the snapshot itself is untouched
    # Tools run as scripts from the copy (npx vitest): /work must allow it for these runs.
    run_script = "printf '#!/bin/sh\\necho ran\\n' > t.sh && chmod +x t.sh && ./t.sh"
    ran = await sandbox.run(
        snapshot, node_command("web", ["sh", "-c", run_script]), image=prepared.image,
        limits=replace(Limits.for_fixes(), work_exec=True),
    )  # fmt: skip
    assert (ran.ok, ran.stdout) == (True, "ran\n"), ran.stderr
