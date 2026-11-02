"""Running a test command against a workspace.

- :class:`SandboxRunner`: in the sandbox (3.7/8.2): no network, the code read-only, limits. What
  a pipeline wrote only ever runs here.
- :class:`LocalRunner`: on this machine, with the bench's installed dependencies linked in. Only
  for the bench's own patches (validating it, the gold and no-op pipelines), whose code is ours.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sensei_engine.sandbox.node import node_command
from sensei_engine.sandbox.runner import Limits, Sandbox, SandboxError
from sensei_evals.fixbench.bench import Command

OUTPUT_TAIL = 4000
_ENV_KEPT = ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT")


@dataclass(frozen=True, slots=True)
class TestResult:
    passed: bool
    exit_code: int | None
    output: str  # the end of stdout and stderr
    seconds: float
    timed_out: bool = False


class TestRunner(Protocol):
    trusted_only: bool  # runs on this machine: never for a pipeline's code

    async def run(self, checkout: Path, command: Command) -> TestResult: ...


def _tail(text: str) -> str:
    return text[-OUTPUT_TAIL:]


@dataclass(slots=True)
class LocalRunner:
    """``python`` in a command is ``python`` (the bench's virtualenv); ``links`` are folders
    linked into each workspace (``frontend/node_modules`` to the installed one)."""

    python: Path | None = None
    links: Mapping[str, Path] = field(default_factory=dict[str, Path])
    trusted_only: bool = True

    async def run(self, checkout: Path, command: Command) -> TestResult:
        for relative, target in self.links.items():
            link = checkout / relative
            if target.is_dir() and not link.exists():
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
        argv = list(command.command)
        if argv[0] == "python" and self.python is not None:
            argv[0] = str(self.python)
        env = {k: v for k, v in os.environ.items() if k in _ENV_KEPT}
        env.update(PYTHONDONTWRITEBYTECODE="1", CI="1", NO_COLOR="1")
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=checkout / command.workdir,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), command.timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()
            return TestResult(False, None, "timed out", time.monotonic() - started, True)
        text = out.decode("utf-8", errors="replace")
        code = process.returncode
        return TestResult(code == 0, code, _tail(text), time.monotonic() - started)


@dataclass(slots=True)
class SandboxRunner:
    """In a fresh sandbox container with a fix's limits, from the image with the dependencies
    of the folder the command runs in (``images``, by workdir); a JavaScript folder's
    (``node``) through the Node image's launcher, in a writable copy."""

    sandbox: Sandbox
    images: Mapping[str, str]
    node: frozenset[str] = frozenset()
    trusted_only: bool = False

    async def run(self, checkout: Path, command: Command) -> TestResult:
        image = self.images.get(command.workdir)
        if image is None:
            return TestResult(False, None, f"no sandbox image for {command.workdir!r}", 0.0)
        is_node = command.workdir in self.node
        argv = node_command(command.workdir, command.command) if is_node else command.command
        try:
            result = await self.sandbox.run(
                checkout,
                argv,
                image=image,
                time_limit=command.timeout_seconds,
                workdir="" if is_node else command.workdir,
                env={"PYTHONDONTWRITEBYTECODE": "1", "CI": "1", "NO_COLOR": "1"},
                limits=Limits.for_fixes(),
            )
        except SandboxError as error:
            return TestResult(False, None, f"the sandbox failed: {error}", 0.0)
        output = _tail(f"{result.stdout}\n{result.stderr}".strip())
        return TestResult(result.ok, result.exit_code, output, result.duration, result.timed_out)
