"""Sandbox hardening (8.2) without Docker: the registry proxy's rules (the registries only, HTTPS
only, never a private address), gVisor where it's required, the limits of a fix's runs, the
database sidecar's container, and which JavaScript projects can be installed."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from sensei_engine.sandbox.node import detect_node, node_command
from sensei_engine.sandbox.proxy import ALLOWED, Proxy, parse_connect, public
from sensei_engine.sandbox.runner import Limits, Sandbox, SandboxUnavailableError, run_argv
from sensei_engine.sandbox.sidecars import postgres_argv, postgres_env


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n", ("pypi.org", 443)),
        (b"CONNECT Files.PythonHosted.org.:443 HTTP/1.1\r\n\r\n", ("files.pythonhosted.org", 443)),
        (b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n", ("registry.npmjs.org", 443)),
        (b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", "403 Forbidden"),
        (b"CONNECT pypi.org:22 HTTP/1.1\r\n\r\n", "403 Forbidden"),
        (b"CONNECT pypi.org.evil.com:443 HTTP/1.1\r\n\r\n", "403 Forbidden"),
        (b"GET http://pypi.org/simple/ HTTP/1.1\r\n\r\n", "405 Method Not Allowed"),
        (b"CONNECT pypi.org HTTP/1.1\r\n\r\n", "400 Bad Request"),
        (b"\xff\xfe garbage\r\n\r\n", "400 Bad Request"),
    ],
)
def test_the_proxy_lets_through_the_registries_over_https_only(
    head: bytes, expected: object
) -> None:
    assert parse_connect(head, ALLOWED) == expected


@pytest.mark.parametrize(
    ("address", "ok"),
    [
        ("151.101.0.223", True),
        ("2a04:4e42::223", True),
        ("127.0.0.1", False),
        ("10.0.0.5", False),
        ("172.17.0.1", False),  # the Docker host
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # a cloud's metadata service
        ("::1", False),
        ("fd00::1", False),
        ("224.0.0.1", False),
        ("not an ip", False),
    ],
)
def test_only_public_addresses_are_reached(address: str, ok: bool) -> None:
    assert public(address) is ok


async def _upstream() -> tuple[asyncio.Server, int]:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"upstream:" + await reader.read(100))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _through(proxy: Proxy, request: bytes) -> bytes:
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        if answer.startswith(b"HTTP/1.1 200"):
            writer.write(b"hello")
            await writer.drain()
            answer += await asyncio.wait_for(reader.read(100), 5)
        writer.close()
        return answer
    finally:
        server.close()
        await server.wait_closed()


async def test_the_proxy_tunnels_to_an_allowed_host_and_refuses_private_addresses() -> None:
    upstream, port = await _upstream()

    async def to_upstream(_host: str, _port: int) -> str:
        return "127.0.0.1"

    try:
        tunnel = Proxy({"pkg.test"}, port=port, resolver=to_upstream)
        answer = await _through(tunnel, f"CONNECT pkg.test:{port} HTTP/1.1\r\n\r\n".encode())
        assert answer.startswith(b"HTTP/1.1 200 Connection Established")
        assert answer.endswith(b"upstream:hello")
        # The real resolver: an allowed name that points at this machine is refused.
        rebound = Proxy({"localhost"}, port=port)
        answer = await _through(rebound, f"CONNECT localhost:{port} HTTP/1.1\r\n\r\n".encode())
        assert answer.startswith(b"HTTP/1.1 502")
        refused = await _through(tunnel, b"CONNECT other.test:443 HTTP/1.1\r\n\r\n")
        assert refused.startswith(b"HTTP/1.1 403")
    finally:
        upstream.close()
        with contextlib.suppress(Exception):
            await upstream.wait_closed()


async def test_gvisor_is_required_where_it_is_set(tmp_path: Path) -> None:
    strict = Sandbox(require_gvisor=True)
    with pytest.raises(SandboxUnavailableError, match="gVisor"):
        await strict.run(tmp_path, ["true"])
    Sandbox(require_gvisor=True, runtime="runsc").check_isolation()  # set: fine
    Sandbox().check_isolation()  # development: a warning, once


def test_a_fixs_runs_get_the_fix_limits(tmp_path: Path) -> None:
    limits = Limits.for_fixes()
    argv = run_argv(
        binary="docker", name="n", image="i", source=tmp_path, workdir="/src", cmd=["pytest"],
        env={}, limits=limits, runtime=None, stdin=False, time_limit=limits.timeout_seconds,
        now=0.0, network="sensei-run-abc",
    )  # fmt: skip
    for flag in ("--cpus=2", "--memory=4096m", "--memory-swap=4096m", "--pids-limit=512",
                 "--network=sensei-run-abc", "--read-only", "--cap-drop=ALL"):  # fmt: skip
        assert flag in argv
    assert any(a.startswith("--tmpfs=/work:") and "size=2048m" in a for a in argv)
    assert limits.max_output_bytes == 5 * 1024 * 1024
    assert limits.timeout_seconds == 600
    assert not any("docker.sock" in a for a in argv)


def test_the_database_sidecar_is_on_the_runs_network_only() -> None:
    argv = postgres_argv("docker", "sensei-sandbox-db-x", "sensei-run-x", "postgres:17", 0.0)
    assert "--network=sensei-run-x" in argv
    assert "--network-alias=db" in argv
    assert "--pull=never" in argv
    assert "--cap-drop=ALL" in argv
    assert not any(a.startswith(("--publish", "-p")) for a in argv)  # nothing reaches it
    assert any(a.startswith("--tmpfs=/var/lib/postgresql/data") for a in argv)
    assert postgres_env()["DATABASE_URL"] == "postgresql://sandbox:sandbox@db:5432/app"


def test_only_npm_locked_projects_install(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    assert detect_node(tmp_path, "web").package_json is None  # nothing to install
    (tmp_path / "web" / "package.json").write_text('{"name": "web"}')
    unlocked = detect_node(tmp_path, "web")
    assert not unlocked.installable
    assert unlocked.limitation is not None
    assert "aren't pinned" in unlocked.limitation
    (tmp_path / "web" / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    assert "pnpm-lock.yaml found" in (detect_node(tmp_path, "web").limitation or "")
    (tmp_path / "web" / "package-lock.json").write_text('{"lockfileVersion": 3}')
    locked = detect_node(tmp_path, "web")
    assert locked.installable
    # The image is the lock's: another lock (or another runner image) is another image.
    (tmp_path / "web" / "package-lock.json").write_text('{"lockfileVersion": 3, "x": 1}')
    relocked = detect_node(tmp_path, "web")
    assert locked.key("base") != relocked.key("base")
    assert locked.key("base") != locked.key("other base")
    assert node_command("web", ["npx", "vitest", "run"]) == [
        "sensei-node-run", "web", "npx", "vitest", "run",
    ]  # fmt: skip


def test_a_symlinked_lockfile_is_not_read(tmp_path: Path) -> None:
    (tmp_path / "secret").write_text("{}")
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").symlink_to(tmp_path / "secret")
    assert detect_node(tmp_path).lock is None
