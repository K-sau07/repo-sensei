"""The test runners the CLI builds for a bench: on this machine (the bench's virtualenv in
``.cache/venv``, each ``node_modules`` installed in the app linked in) or in the sandbox (an
image with each folder's dependencies, Python's or npm's)."""

from __future__ import annotations

from pathlib import Path

from sensei_core.config import SandboxSettings
from sensei_engine.sandbox.deps import DependencyImages, detect
from sensei_engine.sandbox.node import NodeImages, detect_node
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
    python = DependencyImages.from_settings(sandbox, settings)
    node = NodeImages(sandbox)
    built: dict[str, str] = {}
    node_dirs: set[str] = set()
    for command in bench.spec.suites.values():
        folder = command.workdir
        if folder in built:
            continue
        js = detect_node(bench.app_dir, folder)
        if js.package_json is not None:
            prepared_node = await node.prepare(js)
            if not prepared_node.installed:
                raise BenchError(f"the {folder or 'app'} dependencies didn't install: "
                                 f"{prepared_node.limitation}\n{prepared_node.detail}")  # fmt: skip
            built[folder] = prepared_node.image
            node_dirs.add(folder)
            continue
        prepared = await python.prepare(detect(bench.app_dir, folder))
        if not prepared.installed:
            raise BenchError(
                f"the {folder or 'app'} dependencies didn't install: {prepared.limitation}"
                f"\n{prepared.detail}"
            )
        built[folder] = prepared.image
    return SandboxRunner(sandbox=sandbox, images=built, node=frozenset(node_dirs))


def bench_root(root: Path, suite: str) -> Path | None:
    """``fixbench`` (the private bench) or a path to a bench folder; None if ``suite`` isn't
    one."""
    if suite == "fixbench":
        return paths.fixbench_dir(root)
    candidate = Path(suite)
    return candidate if (candidate / "fixbench.yaml").is_file() else None
