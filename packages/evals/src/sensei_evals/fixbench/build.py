"""The test runners the CLI builds for a bench: on this machine (the bench's virtualenv in
``.cache/venv``, each ``node_modules`` installed in the app linked in) or in the sandbox (an
image with each Python folder's dependencies)."""

from __future__ import annotations

from pathlib import Path

from sensei_core.config import SandboxSettings
from sensei_engine.sandbox.deps import DependencyImages, detect
from sensei_engine.sandbox.runner import Sandbox
from sensei_evals import paths
from sensei_evals.fixbench.bench import Bench, BenchError
from sensei_evals.fixbench.runners import LocalRunner, SandboxRunner


def local_runner(bench: Bench) -> LocalRunner:
    python = bench.root / ".cache" / "venv" / "bin" / "python"
    app = bench.app_dir
    installed = [*app.glob("node_modules"), *app.glob("*/node_modules")]
    links = {str(path.relative_to(app)): path for path in installed if path.is_dir()}
    return LocalRunner(python=python if python.is_file() else None, links=links)


async def sandbox_runner(bench: Bench, settings: SandboxSettings) -> SandboxRunner:
    sandbox = Sandbox.from_settings(settings)
    images = DependencyImages.from_settings(sandbox, settings)
    built: dict[str, str] = {}
    for command in bench.spec.suites.values():
        deps = detect(bench.app_dir, command.workdir)
        if deps.empty or command.workdir in built:
            continue
        prepared = await images.prepare(deps)
        if not prepared.installed:
            raise BenchError(
                f"the {command.workdir or 'app'} dependencies didn't install: {prepared.limitation}"
            )
        built[command.workdir] = prepared.image
    return SandboxRunner(sandbox=sandbox, images=built)


def bench_root(root: Path, suite: str) -> Path | None:
    """``fixbench`` (the private bench) or a path to a bench folder; None if ``suite`` isn't
    one."""
    if suite == "fixbench":
        return paths.fixbench_dir(root)
    candidate = Path(suite)
    return candidate if (candidate / "fixbench.yaml").is_file() else None
