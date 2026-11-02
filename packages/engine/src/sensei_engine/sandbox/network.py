"""The sandbox's networks (8.2).

- ``sensei-sandbox-build``: internal (no route out). Dependency installs run here; their only
  way out is the registry proxy (:mod:`sensei_engine.sandbox.proxy`), reached by its alias
  ``registry-proxy``.
- ``sensei-sandbox-egress``: the proxy's way out.
- per run, ``sensei-run-<id>``: internal, for a run and its sidecars (a throwaway Postgres) to
  reach each other and nothing else. Runs without sidecars have no network at all.

:class:`Networks` creates what's missing, once, and starts the proxy container (read-only, as an
unprivileged user, no capabilities) when it isn't running.
"""

from __future__ import annotations

import asyncio
import contextlib

from sensei_engine.sandbox.runner import LABEL, Sandbox, SandboxUnavailableError, docker_env

BUILD_NETWORK = "sensei-sandbox-build"
EGRESS_NETWORK = "sensei-sandbox-egress"
PROXY_CONTAINER = "sensei-registry-proxy"
PROXY_ALIAS = "registry-proxy"
PROXY_IMAGE = "repo-sensei/registry-proxy:1"
PROXY_PORT = 3128
PROXY_URL = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
READY_SECONDS = 15.0
_PROBE = f"import socket; socket.create_connection(('127.0.0.1', {PROXY_PORT}), timeout=2)"


class Networks:
    def __init__(self, sandbox: Sandbox, *, proxy_image: str = PROXY_IMAGE) -> None:
        self._sandbox = sandbox
        self._proxy_image = proxy_image
        self._lock = asyncio.Lock()

    async def _docker(self, *args: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            self._sandbox.docker(),
            *args,
            env=docker_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await process.communicate()
        return process.returncode or 0, out.decode("utf-8", "replace").strip()

    async def _network(self, name: str, *, internal: bool) -> None:
        code, found = await self._docker("network", "inspect", "--format={{.Internal}}", name)
        if code == 0:
            # One made by hand without --internal would give installs a way out: refused.
            if internal and found.strip() != "true":
                raise SandboxUnavailableError(
                    f"the {name} network isn't internal: remove it (docker network rm {name})"
                )
            return
        flags = ["--internal"] if internal else []
        code, said = await self._docker(
            "network", "create", *flags, f"--label={LABEL}=network", name
        )
        if code != 0 and "already exists" not in said:
            raise SandboxUnavailableError(f"couldn't create the {name} network: {said[-300:]}")

    async def proxy(self) -> str:
        """The proxy's URL, from the build network (made ready first)."""
        async with self._lock:
            await self._network(BUILD_NETWORK, internal=True)
            await self._network(EGRESS_NETWORK, internal=False)
            code, _ = await self._docker("exec", PROXY_CONTAINER, "python", "-c", _PROBE)
            if code == 0:
                return PROXY_URL
            # Not there, stopped, or failing: a fresh one.
            await self._docker("rm", "--force", PROXY_CONTAINER)
            await self._start_proxy()
        return PROXY_URL

    async def _start_proxy(self) -> None:
        code, said = await self._docker(
            "run", "--detach", f"--name={PROXY_CONTAINER}", f"--label={LABEL}=proxy",
            "--pull=never", f"--network={EGRESS_NETWORK}", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=10001:10001", "--memory=256m",
            "--pids-limit=128", "--restart=unless-stopped", self._proxy_image,
        )  # fmt: skip
        if code != 0:
            raise SandboxUnavailableError(
                f"couldn't start the registry proxy ({self._proxy_image}; build it with "
                f"`make sandbox-image`): {said[-300:]}"
            )
        code, said = await self._docker(
            "network", "connect", f"--alias={PROXY_ALIAS}", BUILD_NETWORK, PROXY_CONTAINER
        )
        if code != 0:
            raise SandboxUnavailableError(f"couldn't attach the registry proxy: {said[-300:]}")
        deadline = asyncio.get_running_loop().time() + READY_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            # Ready when it takes a connection (running alone could be a restart loop).
            code, _ = await self._docker("exec", PROXY_CONTAINER, "python", "-c", _PROBE)
            if code == 0:
                return
            await asyncio.sleep(0.3)
        _, logs = await self._docker("logs", "--tail=5", PROXY_CONTAINER)
        raise SandboxUnavailableError(f"the registry proxy didn't start: {logs[-300:]}")

    async def run_network(self, name: str) -> str:
        """A fresh internal network for one run and its sidecars."""
        code, said = await self._docker(
            "network", "create", "--internal", f"--label={LABEL}=run", name
        )
        if code != 0:
            raise SandboxUnavailableError(f"couldn't create the run's network: {said[-300:]}")
        return name

    async def remove(self, name: str) -> None:
        with contextlib.suppress(OSError):
            await self._docker("network", "rm", name)
