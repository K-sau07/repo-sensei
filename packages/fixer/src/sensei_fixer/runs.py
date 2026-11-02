"""Running a fix's tests (8.6, 8.7): in the sandbox, in the fix's checkout.

:func:`sandbox_runner` makes the project's dependencies image (Python's or npm's, behind the
registry proxy), starts a throwaway Postgres when the repository's compose file has one (the
tests' database: on the run's own network, nothing else reachable), and gives a runner that
runs an argv from the project's folder with a fix's limits. A JavaScript run goes through the
Node image's launcher, in a writable copy (its tools' scripts allowed to run there).

What a run gives back is its exit, its output's end (redacted, as every sandbox run's is) and
how long it took, which is what a fix spends of its sandbox budget.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from sensei_core.config import SandboxSettings
from sensei_engine.sandbox.deps import DependencyImages, detect
from sensei_engine.sandbox.network import Networks
from sensei_engine.sandbox.node import NodeImages, detect_node, node_command
from sensei_engine.sandbox.runner import Limits, Sandbox
from sensei_engine.sandbox.sidecars import postgres
from sensei_engine.snapshots import read_file
from sensei_fixer.frameworks import TestSetup

OUTPUT_TAIL = 6_000
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
TEST_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"}


class SetupFailed(Exception):
    """The project's tests can't run here (its dependencies didn't install: why)."""


@dataclass(frozen=True, slots=True)
class RunResult:
    passed: bool
    exit_code: int | None
    output: str  # the end of stdout and stderr
    seconds: float
    timed_out: bool = False


class FixRunner(Protocol):
    async def run(self, argv: Sequence[str]) -> RunResult: ...


def needs_postgres(root: Path, project: str) -> bool:
    """The repository's compose file runs Postgres (its tests likely want one)."""
    for folder in dict.fromkeys((project, "")):
        for name in COMPOSE_FILES:
            path = f"{folder}/{name}" if folder else name
            try:
                text = read_file(root, path, max_bytes=512 * 1024).decode("utf-8", "replace")
            except (OSError, ValueError):
                continue
            if (
                "image: postgres" in text
                or "image: 'postgres" in text
                or 'image: "postgres' in text
            ):
                return True
    return False


@dataclass
class SandboxFixRunner:
    sandbox: Sandbox
    image: str
    root: Path
    setup: TestSetup
    network: str | None = None
    env: dict[str, str] | None = None

    async def run(self, argv: Sequence[str]) -> RunResult:
        node = self.setup.language == "javascript"
        cmd = node_command(self.setup.project, argv) if node else list(argv)
        limits = replace(Limits.for_fixes(), work_exec=node)
        result = await self.sandbox.run(
            self.root, cmd, image=self.image, workdir="" if node else self.setup.project,
            env={**TEST_ENV, **(self.env or {})}, limits=limits, network=self.network,
        )  # fmt: skip
        output = f"{result.stdout}\n{result.stderr}".strip()[-OUTPUT_TAIL:]
        return RunResult(result.ok, result.exit_code, output, result.duration, result.timed_out)


@asynccontextmanager
async def sandbox_runner(
    settings: SandboxSettings, root: Path, setup: TestSetup
) -> AsyncGenerator[SandboxFixRunner]:
    sandbox = Sandbox.from_settings(settings)
    networks = Networks(sandbox)
    if setup.language == "javascript":
        prepared = await NodeImages(sandbox, networks=networks).prepare(
            detect_node(root, setup.project)
        )
    else:
        prepared = await DependencyImages.from_settings(sandbox, settings).prepare(
            detect(root, setup.project)
        )
    if not prepared.installed:
        raise SetupFailed(f"The project's dependencies didn't install: {prepared.limitation}")
    async with AsyncExitStack() as stack:
        network = env = None
        if needs_postgres(root, setup.project):
            services = await stack.enter_async_context(postgres(sandbox, networks))
            network, env = services.network, services.env
        yield SandboxFixRunner(sandbox, prepared.image, root, setup, network, env)
