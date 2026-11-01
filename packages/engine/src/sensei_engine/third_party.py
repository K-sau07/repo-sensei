"""Third-party packages in use (6.5): the declared packages (discovery's inventory) joined with
what the code imports (the graph's package and external-symbol nodes): each package's versions,
where it is declared, how many files import it and which.

A Python import names a module, not a distribution: ``yaml`` is ``PyYAML``. Names are matched
normalized (PEP 503), then through :data:`IMPORT_NAMES` for the common ones that differ. What is
imported but declared nowhere is listed apart (``undeclared``), the standard library and Node's
built-ins left out.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from packaging.utils import canonicalize_name

from sensei_core.models import NodeKind
from sensei_engine.graph.snapshot import GraphSnapshot

IMPORT_NAMES: Mapping[str, tuple[str, ...]] = {
    "yaml": ("pyyaml",),
    "pil": ("pillow",),
    "bs4": ("beautifulsoup4",),
    "jwt": ("pyjwt",),
    "dateutil": ("python-dateutil",),
    "dotenv": ("python-dotenv",),
    "jose": ("python-jose",),
    "multipart": ("python-multipart",),
    "sklearn": ("scikit-learn",),
    "cv2": ("opencv-python", "opencv-python-headless"),
    "psycopg2": ("psycopg2", "psycopg2-binary"),
    "magic": ("python-magic",),
    "slugify": ("python-slugify",),
    "socketio": ("python-socketio",),
    "google": ("protobuf", "google-cloud-storage", "google-api-python-client"),
}
NODE_BUILTINS = frozenset(
    {
        "assert", "buffer", "child_process", "cluster", "crypto", "dns", "events", "fs", "http",
        "http2", "https", "net", "os", "path", "process", "querystring", "readline", "stream",
        "string_decoder", "timers", "tls", "url", "util", "v8", "vm", "worker_threads", "zlib",
    }
)  # fmt: skip
USED_IN = 20  # files listed per package


@dataclass(slots=True)
class Usage:
    files: Counter[str] = field(default_factory=Counter[str])  # importing file → its imports

    @property
    def imports(self) -> int:
        return sum(self.files.values())


def _imported(key: str) -> tuple[str, str] | None:
    """A package or external-symbol node's ecosystem and package (or top module) name."""
    kind, _, rest = key.partition(" ")
    prefix, _, name = rest.partition(":")
    if prefix not in ("py", "npm") or not name:
        return None
    if kind == "external_symbol":
        if prefix == "py":
            name = name.split("/", 1)[0]
        else:  # "pkg/name" or "@scope/pkg/name"
            parts = name.split("/")
            name = "/".join(parts[:2]) if name.startswith("@") else parts[0]
    if prefix == "py":
        return "pypi", name.split(".", 1)[0]
    return "npm", name.removeprefix("node:")


def usage(graph: GraphSnapshot) -> dict[tuple[str, str], Usage]:
    """What the code imports from outside, by (ecosystem, import name): the importing files."""
    found: defaultdict[tuple[str, str], Usage] = defaultdict(Usage)
    outside = {NodeKind.PACKAGE.value, NodeKind.EXTERNAL_SYMBOL.value}
    for a, b, _ in graph.graph.weighted_edge_list():
        target = graph.nodes[b]
        if target.kind not in outside:
            continue
        named = _imported(target.symbol_key)
        source = graph.nodes[a]
        if named is not None and source.path:
            found[named].files[source.path] += 1
    return dict(found)


@dataclass(slots=True)
class PackageInUse:
    ecosystem: str
    name: str
    declared: list[Mapping[str, Any]]  # the inventory's entries for it
    usage: Usage

    @property
    def versions(self) -> list[str]:
        return sorted({str(d["version"]) for d in self.declared if d.get("version")})

    @property
    def dev(self) -> bool:
        return bool(self.declared) and all(bool(d.get("dev")) for d in self.declared)


def _candidates(ecosystem: str, imported: str) -> list[str]:
    if ecosystem == "npm":
        return [imported]
    return [canonicalize_name(imported), *IMPORT_NAMES.get(imported.lower(), ())]


def _builtin(ecosystem: str, imported: str) -> bool:
    if ecosystem == "pypi":
        return imported in sys.stdlib_module_names or imported.startswith("_")
    return imported in NODE_BUILTINS


def packages_in_use(
    declared: Sequence[Mapping[str, Any]], used: Mapping[tuple[str, str], Usage]
) -> tuple[list[PackageInUse], list[PackageInUse]]:
    """The declared packages with their usage (the most used first), and what is imported but
    declared nowhere."""
    by_name: dict[tuple[str, str], PackageInUse] = {}
    for entry in declared:
        key = (str(entry.get("ecosystem")), str(entry.get("name")))
        held = by_name.get(key)
        if held is None:
            held = by_name[key] = PackageInUse(key[0], key[1], [], Usage())
        held.declared.append(entry)
    undeclared: list[PackageInUse] = []
    for (ecosystem, imported), counts in sorted(used.items()):
        match = next(
            (
                by_name[(ecosystem, name)]
                for name in _candidates(ecosystem, imported)
                if (ecosystem, name) in by_name
            ),
            None,
        )
        if match is not None:
            match.usage.files.update(counts.files)
        elif not _builtin(ecosystem, imported):
            undeclared.append(PackageInUse(ecosystem, imported, [], counts))

    def rank(p: PackageInUse) -> tuple[int, int, str, str]:
        return (-len(p.usage.files), -p.usage.imports, p.ecosystem, p.name)

    return sorted(by_name.values(), key=rank), sorted(undeclared, key=rank)
