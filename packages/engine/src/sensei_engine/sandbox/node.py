"""A JavaScript project's dependencies, installed into an image its sandboxed runs start from
(8.2), like :mod:`sensei_engine.sandbox.deps` for Python.

:func:`detect_node` reads ``package.json`` and ``package-lock.json`` from the unpacked snapshot
(never running anything); only npm's lockfile is used (a pnpm or yarn project is a limitation for
now). :meth:`NodeImages.prepare` installs them with ``npm ci --ignore-scripts`` (no package's
install scripts run) in a container from the Node runner image, on the internal build network
behind the registry proxy, and commits it: the image is tagged by a hash of the two files and
the runner image's id, so the same lockfile reuses it.

A run goes through the image's launcher (:func:`node_command`): the snapshot is read-only and
test tools write, so it runs in a copy, with the installed ``node_modules`` copied in.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sensei_engine.sandbox.deps import install_output
from sensei_engine.sandbox.network import BUILD_NETWORK, INSTALL_CONCURRENCY, Networks
from sensei_engine.sandbox.runner import (
    LABEL,
    Sandbox,
    SandboxError,
    SandboxUnavailableError,
    docker_env,
)
from sensei_engine.snapshots import UnsafePathError, check_path, read_file

NODE_IMAGE = "repo-sensei/node-runner:22"
NODE_DEPS_IMAGE = "repo-sensei/sandbox-node-deps"
LAUNCHER = "sensei-node-run"
MOUNT = "/tmp/sensei"  # noqa: S108 - inside the install's container, mounted read-only
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_LOCK_BYTES = 64 * 1024 * 1024
INSTALL_TIMEOUT_SECONDS = 900.0
RECIPE = "npm-ci-ignore-scripts-2"
INSTALLER = "10002:10002"  # the Node image's installer, who owns /opt/deps (never root)
# Copy the two files to where node_modules goes, then install exactly the lock, no scripts.
_INSTALL = (
    f"cp {MOUNT}/package.json {MOUNT}/package-lock.json /opt/deps/ && cd /opt/deps && "
    "npm ci --ignore-scripts --no-audit --no-fund --loglevel=error "
    f"--maxsockets={INSTALL_CONCURRENCY}"
)


@dataclass(frozen=True, slots=True)
class NodeDependencies:
    project: str  # the directory in the snapshot ("" is the root)
    package_json: bytes | None
    lock: bytes | None
    limitation: str | None = None  # why it can't be installed (then nothing is)

    @property
    def installable(self) -> bool:
        return self.package_json is not None and self.lock is not None and not self.limitation

    def key(self, base_image_id: str) -> str:
        digest = hashlib.sha256()
        for part in (base_image_id.encode(), RECIPE.encode(), self.package_json or b"",
                     self.lock or b""):  # fmt: skip
            digest.update(len(part).to_bytes(8, "big") + part)
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedNode:
    image: str
    installed: bool
    limitation: str | None = None
    detail: str = ""


def _join(project: str, name: str) -> str:
    return f"{project}/{name}" if project else name


def _read(root: Path, path: str, limit: int) -> bytes | None:
    try:
        data = read_file(root, path, max_bytes=limit)
    except (OSError, ValueError):  # missing, a folder, a symbolic link, too large, unsafe
        return None
    return data


def detect_node(root: Path, project: str = "") -> NodeDependencies:
    """The npm dependencies of the project in ``project`` (a folder of the snapshot)."""
    try:
        project = check_path(project)
    except UnsafePathError as exc:
        raise SandboxError(f"not a directory in the snapshot: {project!r}") from exc
    package = _read(root, _join(project, "package.json"), MAX_MANIFEST_BYTES)
    lock = _read(root, _join(project, "package-lock.json"), MAX_LOCK_BYTES)
    limitation = None
    if package is not None and lock is None:
        others = [n for n in ("pnpm-lock.yaml", "yarn.lock")
                  if _read(root, _join(project, n), MAX_LOCK_BYTES) is not None]  # fmt: skip
        limitation = (
            f"only npm's package-lock.json is installed for now ({others[0]} found)"
            if others
            else "no package-lock.json: the versions to install aren't pinned"
        )
    return NodeDependencies(project, package, lock, limitation)


def node_command(project: str, cmd: Sequence[str]) -> list[str]:
    """``cmd`` run by the launcher in ``project`` of a writable copy of the snapshot."""
    return [LAUNCHER, project or ".", *cmd]


class NodeImages:
    def __init__(
        self,
        sandbox: Sandbox,
        *,
        base: str = NODE_IMAGE,
        repository: str = NODE_DEPS_IMAGE,
        timeout: float = INSTALL_TIMEOUT_SECONDS,
        networks: Networks | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._base = base
        self._repository = repository
        self._timeout = timeout
        self._networks = networks or Networks(sandbox)
        self._building: dict[str, asyncio.Lock] = {}

    async def prepare(self, deps: NodeDependencies) -> PreparedNode:
        """The image for ``deps`` (built when it isn't there). Never raises for a project whose
        dependencies don't install (that's ``limitation``)."""
        base_id = await self._sandbox.image_id(self._base)
        if base_id is None:
            raise SandboxUnavailableError(
                f"no {self._base} image: build it with `make sandbox-image`"
            )
        if deps.package_json is None:
            return PreparedNode(image=self._base, installed=True)
        if not deps.installable:
            return PreparedNode(image=self._base, installed=False, limitation=deps.limitation)
        tag = f"{self._repository}:{deps.key(base_id)[:32]}"
        async with self._building.setdefault(tag, asyncio.Lock()):
            if await self._sandbox.image_id(tag) is not None:
                return PreparedNode(image=tag, installed=True)
            try:
                async with asyncio.timeout(self._timeout):
                    ok, detail = await self._build(tag, deps)
            except TimeoutError:
                limitation = f"installing the dependencies took over {self._timeout:g} s"
                return PreparedNode(image=self._base, installed=False, limitation=limitation)
        if not ok:
            return PreparedNode(
                image=self._base, installed=False, detail=detail,
                limitation="the dependencies didn't install (see the detail)",
            )  # fmt: skip
        return PreparedNode(image=tag, installed=True, detail=detail)

    async def _build(self, tag: str, deps: NodeDependencies) -> tuple[bool, str]:
        proxy = await self._networks.proxy()
        name = f"sensei-node-deps-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="sensei-node-deps-") as context:
            folder = Path(context)
            await asyncio.to_thread(_write_manifests, folder, deps)
            # The proxy for the install's process only: as the container's environment it would
            # be committed into the image.
            argv = [
                self._sandbox.docker(), "run", f"--name={name}", f"--label={LABEL}=deps-build",
                "--pull=never", f"--network={BUILD_NETWORK}", f"--user={INSTALLER}",
                *self._sandbox.runtime_args(), "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--memory=4096m", "--memory-swap=4096m",
                "--pids-limit=512", f"--mount=type=bind,source={folder},target={MOUNT},readonly",
                self._base, "env", f"HTTPS_PROXY={proxy}", f"HTTP_PROXY={proxy}", "HOME=/tmp",
                "npm_config_cache=/tmp/.npm", "sh", "-c", _INSTALL,
            ]  # fmt: skip
            try:
                code, out = await self._exec(argv)
                if code == 0:
                    code, said = await self._exec(
                        [
                            self._sandbox.docker(),
                            "commit",
                            "--change=USER 10001:10001",
                            # The install's command isn't the image's: back to the base's.
                            '--change=CMD ["node"]',
                            f"--change=LABEL {LABEL}=deps",
                            name,
                            tag,
                        ]
                    )
                    if code != 0:
                        out += b"\n" + said
            finally:
                with contextlib.suppress(OSError):
                    await self._exec([self._sandbox.docker(), "rm", "--force", name])
        return code == 0, install_output(out)

    async def _exec(self, argv: list[str]) -> tuple[int, bytes]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=docker_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await process.communicate()
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                await process.wait()
        return process.returncode or 0, out


def _write_manifests(folder: Path, deps: NodeDependencies) -> None:
    (folder / "package.json").write_bytes(deps.package_json or b"")
    (folder / "package-lock.json").write_bytes(deps.lock or b"")
    folder.chmod(0o755)  # read by the install's container, as the installer
    for name in ("package.json", "package-lock.json"):
        (folder / name).chmod(0o644)
