"""Running a repository's own code in a throwaway Docker container (3.7).

Each :meth:`Sandbox.run` starts a fresh container from the runner image (or a repository's
dependencies image, :mod:`sensei_engine.sandbox.deps`) and removes it afterwards, whatever
happens. It goes through the ``docker`` CLI, one argument at a time and never through a shell.

What a run gets:

- the unpacked snapshot **read-only** at ``/src`` (a real directory: a symbolic link is refused),
  and an empty writable ``/work`` (also ``$HOME``) and ``/tmp``, both in memory and size-capped;
  the image itself is read-only;
- **no network** (``--network none``);
- a non-root user, no capabilities, no privilege escalation, an init process to reap children;
- CPU, memory (no swap on top) and process-count limits;
- only the environment variables the caller passes (placeholders, never secrets).

What it gives back: the exit code, stdout and stderr (each capped, with credentials masked by
:func:`sensei_core.redaction.redact_text`) and how long it took. A run that's still going at its
time limit, or that writes more than the cap, is killed. Docker keeps no logs of it
(``--log-driver none``): the output is read from the attached CLI as it comes.

The time limit holds even if the container outlives the CLI that started it (a daemon that
created it only after the run gave up and removed nothing): the command runs under
``/usr/bin/timeout --signal=KILL`` inside the container, a few seconds past the limit, and each
container carries its deadline (counted from when it starts) in a label, so
:meth:`Sandbox.sweep` (before every run) removes any still there after it.

Hardening for runs whose results reach a fix (gVisor, a registry proxy, disk quotas) is 8.2;
``runtime="runsc"`` already selects gVisor where the host has it.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sensei_core.config import SandboxSettings
from sensei_core.redaction import redact_text
from sensei_engine.snapshots import check_path

DOCKER_BINARY = "docker"
RUNNER_IMAGE = "repo-sensei/python-runner"  # tagged by Python version, e.g. ":3.13"
DEFAULT_PYTHON = "3.13"
SANDBOX_UID = 10001  # the image's sandbox user
NAME_PREFIX = "sensei-sandbox-"
LABEL = "dev.repo-sensei.sandbox"
DEADLINE_LABEL = f"{LABEL}.deadline"  # when it must be gone, in seconds since the epoch
SWEEP_BYTES = 1024 * 1024  # of the containers' listing
INSIDE_GRACE_SECONDS = 5  # the container's own limit, past the run's (which kills it first)
TIMEOUT_BINARY = "/usr/bin/timeout"  # coreutils', in the image (not whatever PATH finds first)
MAX_ARGS = 256
MAX_ARG_CHARS = 64 * 1024
MAX_ENV = 64
STDERR_BYTES = 8192  # of docker's own complaints
DOCKER_TIMEOUT_SECONDS = 30.0  # a docker command that isn't the run (kill, rm, inspect)
# docker run's own exit codes: it failed (125), the command can't run (126) or isn't there (127).
DOCKER_FAILED = 125

# What the docker CLI needs from the environment to reach the daemon; nothing else reaches it.
_DOCKER_ENV = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "XDG_RUNTIME_DIR",
)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_DEADLINE_RE = re.compile(r"[0-9]+")

Runtime = Literal["runc", "runsc"]


class SandboxUnavailableError(RuntimeError):
    """Docker isn't installed or reachable, or the image is missing."""


class SandboxError(ValueError):
    """A run that can't be started as asked (the message says why)."""


@dataclass(frozen=True, slots=True)
class Limits:
    cpus: float = 2.0
    memory_mb: int = 2048
    pids: int = 256
    timeout_seconds: float = 120.0
    max_output_bytes: int = 1024 * 1024  # of stdout, and again of stderr
    work_mb: int = 256  # /work
    tmp_mb: int = 64  # /tmp


@dataclass(frozen=True, slots=True)
class RunResult:
    exit_code: int | None  # None when the run was killed (time limit, output cap)
    stdout: str
    stderr: str
    duration: float  # seconds, the container's start included
    timed_out: bool = False
    truncated: bool = False  # it wrote more than the cap: the rest was dropped and it was killed

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.truncated


def docker_env() -> dict[str, str]:
    return {name: os.environ[name] for name in _DOCKER_ENV if name in os.environ}


def runner_image(python: str = DEFAULT_PYTHON, *, repository: str = RUNNER_IMAGE) -> str:
    return f"{repository}:{python}"


def mount_source(root: Path) -> Path:
    """The snapshot directory to mount: a real directory, not a symbolic link to one."""
    if root.is_symlink() or not root.is_dir():
        raise SandboxError(f"{root} is not a directory")
    return root.resolve(strict=True)


def container_workdir(path: str) -> str:
    try:
        relative = check_path(path)
    except ValueError as exc:
        raise SandboxError(f"not a path in the snapshot: {path!r}") from exc
    return f"/src/{relative}" if relative else "/src"


def _check_command(cmd: Sequence[str]) -> list[str]:
    argv = list(cmd)
    if not argv or not argv[0]:
        raise SandboxError("Give a command to run.")
    if len(argv) > MAX_ARGS or any(len(arg) > MAX_ARG_CHARS or "\0" in arg for arg in argv):
        raise SandboxError(f"At most {MAX_ARGS} arguments of {MAX_ARG_CHARS} characters.")
    return argv


def _check_env(env: Mapping[str, str]) -> list[str]:
    if len(env) > MAX_ENV:
        raise SandboxError(f"At most {MAX_ENV} environment variables.")
    flags: list[str] = []
    for name, value in sorted(env.items()):
        if not _ENV_NAME_RE.fullmatch(name) or "\0" in value or len(value) > MAX_ARG_CHARS:
            raise SandboxError(f"Not an environment variable to set: {name!r}")
        flags.append(f"--env={name}={value}")
    return flags


def run_argv(
    *,
    binary: str,
    name: str,
    image: str,
    source: Path,
    workdir: str,
    cmd: Sequence[str],
    env: Mapping[str, str],
    limits: Limits,
    runtime: Runtime | None,
    stdin: bool,
    time_limit: float,
    now: float,
) -> list[str]:
    """``docker run`` for one sandboxed command (``source`` already checked) that may run for
    ``time_limit`` seconds from ``now`` (the epoch's)."""
    inside = math.ceil(time_limit) + INSIDE_GRACE_SECONDS
    deadline = math.ceil(now + inside + DOCKER_TIMEOUT_SECONDS)
    argv = [
        binary,
        "run",
        "--rm",
        f"--name={name}",
        f"--label={LABEL}=run",
        f"--label={DEADLINE_LABEL}={deadline}",
        "--pull=never",  # only images built here; a missing one is an error, not a download
        "--network=none",
        "--read-only",
        f"--mount=type=bind,source={source},target=/src,readonly",
        f"--tmpfs=/work:rw,nosuid,nodev,size={limits.work_mb}m,uid={SANDBOX_UID},gid={SANDBOX_UID}",
        f"--tmpfs=/tmp:rw,nosuid,nodev,size={limits.tmp_mb}m",
        f"--user={SANDBOX_UID}:{SANDBOX_UID}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--init",
        f"--cpus={limits.cpus:g}",
        f"--memory={limits.memory_mb}m",
        f"--memory-swap={limits.memory_mb}m",  # the same as --memory: no swap on top
        f"--pids-limit={limits.pids}",
        "--ulimit=nofile=1024:1024",
        "--log-driver=none",
        f"--workdir={workdir}",
        "--env=HOME=/work",
        *_check_env(env),
    ]
    if runtime is not None:
        argv.append(f"--runtime={runtime}")
    if stdin:
        argv.append("--interactive")
    # After the image, every argument is the command's (timeout stops reading options at its
    # duration: a command starting with a dash stays the command's).
    argv += [image, TIMEOUT_BINARY, "--signal=KILL", str(inside), *_check_command(cmd)]
    return argv


def expired(listing: str, now: float) -> list[str]:
    """The containers of ``docker ps`` lines (``<name> <deadline>``) past their deadline; one
    with no deadline, or not named as ours, is left alone."""
    found: list[str] = []
    for line in listing.splitlines():
        name, _, deadline = line.strip().partition(" ")
        if (
            name.startswith(NAME_PREFIX)
            and _DEADLINE_RE.fullmatch(deadline)
            and int(deadline) < now
        ):
            found.append(name)
    return found


async def _read_capped(stream: asyncio.StreamReader, cap: int, over: Callable[[], None]) -> bytes:
    """The first ``cap`` bytes; past that, ``over`` is called once and the rest is dropped (so
    the writer never blocks)."""
    kept = bytearray()
    overflowed = False
    while chunk := await stream.read(65536):
        if len(kept) + len(chunk) <= cap:
            kept += chunk
            continue
        kept += chunk[: cap - len(kept)]
        if not overflowed:
            overflowed = True
            over()
    return bytes(kept)


def _text(raw: bytes) -> str:
    return redact_text(raw.decode("utf-8", "replace"))


class Sandbox:
    """Runs commands in fresh containers, at most ``max_concurrent`` at a time (others wait)."""

    def __init__(
        self,
        *,
        binary: str = DOCKER_BINARY,
        image: str = RUNNER_IMAGE,
        runtime: Runtime | None = None,
        limits: Limits | None = None,
        max_concurrent: int = 2,
    ) -> None:
        self.binary = binary
        self.image = image  # the runner image's repository; a tag per Python version
        self.runtime: Runtime | None = runtime
        self.limits = limits or Limits()
        self._slots = asyncio.Semaphore(max_concurrent)

    @classmethod
    def from_settings(cls, settings: SandboxSettings) -> Sandbox:
        return cls(
            binary=settings.docker_binary,
            image=settings.image,
            runtime=settings.runtime,
            limits=Limits(
                cpus=settings.cpus,
                memory_mb=settings.memory_mb,
                pids=settings.pids,
                timeout_seconds=settings.timeout_seconds,
                max_output_bytes=settings.max_output_bytes,
            ),
            max_concurrent=settings.max_concurrent,
        )

    def runner_image(self, python: str = DEFAULT_PYTHON) -> str:
        return runner_image(python, repository=self.image)

    def docker(self) -> str:
        """The docker CLI's path."""
        binary = shutil.which(self.binary)
        if binary is None:
            raise SandboxUnavailableError(f"{self.binary} is not installed")
        return binary

    async def run(
        self,
        root: Path,
        cmd: Sequence[str],
        *,
        image: str | None = None,
        time_limit: float | None = None,
        workdir: str = "",
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> RunResult:
        """Run ``cmd`` (an argv) in a fresh container, with the snapshot unpacked at ``root``
        mounted at ``/src``, in ``/src/<workdir>``, for up to ``time_limit`` seconds (the
        limits' by default). ``image`` is the runner image by default (or a dependencies
        image). :class:`SandboxUnavailableError` when docker can't start it."""
        limits = self.limits
        limit = time_limit or limits.timeout_seconds
        name = NAME_PREFIX + uuid.uuid4().hex
        await self.sweep()
        binary = await asyncio.to_thread(self.docker)
        source = await asyncio.to_thread(mount_source, root)
        async with self._slots:
            # Its deadline counts from when it starts, not from when it queued for a slot: a
            # run that waited isn't already past it for another run's sweep.
            argv = run_argv(
                binary=binary,
                name=name,
                image=image or self.runner_image(),
                source=source,
                workdir=container_workdir(workdir),
                cmd=cmd,
                env=env or {},
                limits=limits,
                runtime=self.runtime,
                stdin=stdin is not None,
                time_limit=limit,
                now=time.time(),
            )
            return await self._run(argv, name, limit, stdin)

    async def _run(
        self, argv: list[str], name: str, time_limit: float, stdin: bytes | None
    ) -> RunResult:
        limits = self.limits
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=docker_env(),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("docker's output isn't piped")
        truncated = timed_out = False
        kills: list[asyncio.Task[None]] = []

        def over() -> None:
            nonlocal truncated
            if not truncated:
                truncated = True
                kills.append(asyncio.create_task(self._kill(name)))

        reads = asyncio.ensure_future(
            asyncio.gather(
                _read_capped(process.stdout, limits.max_output_bytes, over),
                _read_capped(process.stderr, limits.max_output_bytes, over),
            )
        )
        try:
            try:
                async with asyncio.timeout(time_limit):  # a command not reading stdin counts too
                    if stdin is not None and process.stdin is not None:
                        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                            process.stdin.write(stdin)
                            await process.stdin.drain()
                        process.stdin.close()
                    await asyncio.shield(reads)
                    await process.wait()
            except TimeoutError:
                timed_out = True
                await self._kill(name)
            try:
                async with asyncio.timeout(DOCKER_TIMEOUT_SECONDS):  # killed: its output ends
                    out, err = await reads
                    code = await process.wait()
            except TimeoutError:
                raise SandboxUnavailableError("docker didn't stop the container") from None
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            reads.cancel()
            for task in kills:
                await task
            await self._remove(name)  # --rm already did, unless docker or the CLI died first
        duration = time.monotonic() - started
        killed = timed_out or truncated
        if code == DOCKER_FAILED and not killed:
            raise SandboxUnavailableError(f"docker run failed: {_text(err[:STDERR_BYTES])}")
        return RunResult(
            exit_code=None if killed else code,
            stdout=_text(out),
            stderr=_text(err),
            duration=duration,
            timed_out=timed_out,
            truncated=truncated,
        )

    async def _capture(self, *args: str, max_bytes: int = STDERR_BYTES) -> tuple[int, bytes]:
        """A short docker command: its exit code (-1 when it couldn't run or hung) and stdout (its
        first ``max_bytes``)."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.docker(),
                *args,
                env=docker_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, SandboxUnavailableError):
            return -1, b""
        try:
            async with asyncio.timeout(DOCKER_TIMEOUT_SECONDS):
                out, _ = await process.communicate()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return -1, b""
        return process.returncode if process.returncode is not None else -1, out[:max_bytes]

    async def sweep(self) -> int:
        """Remove the sandbox containers past their deadline (see the module), every worker's;
        how many there were."""
        template = f'{{{{.Names}}}} {{{{.Label "{DEADLINE_LABEL}"}}}}'
        code, out = await self._capture(
            "ps", "--all", f"--filter=label={LABEL}", f"--format={template}", max_bytes=SWEEP_BYTES
        )
        if code != 0:
            return 0
        if len(out) == SWEEP_BYTES:  # cut short: the last line may be part of one
            out = out[: out.rfind(b"\n") + 1]
        stale = expired(out.decode("utf-8", "replace"), time.time())
        if stale:
            await self._capture("rm", "--force", *stale)
        return len(stale)

    async def _kill(self, name: str) -> None:
        await self._capture("kill", name)

    async def _remove(self, name: str) -> None:
        await self._capture("rm", "--force", name)

    async def image_id(self, image: str) -> str | None:
        """The local image's id, or None when there's no such image (or no docker)."""
        code, out = await self._capture("image", "inspect", "--format={{.Id}}", image)
        found = out.decode("ascii", "replace").strip()
        return found if code == 0 and found else None
