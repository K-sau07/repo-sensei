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

from sensei_core.models import EdgeType, NodeKind
from sensei_engine.graph.snapshot import GraphSnapshot
from sensei_engine.imports.ecma.resolve import NODE_BUILTINS

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
USED_IN = 20  # files listed per package


@dataclass(slots=True)
class Usage:
    files: Counter[str] = field(default_factory=Counter[str])  # importing file → its imports

    @property
    def imports(self) -> int:
        return sum(self.files.values())


def _imported(key: str) -> tuple[str, str] | None:
    """A package or external-symbol node's ecosystem and what it names: a Python module
    (dotted: ``google.cloud.storage``), or an npm package (``node:fs`` for Node's own)."""
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
        return "pypi", name
    return "npm", name


def usage(graph: GraphSnapshot) -> dict[tuple[str, str], Usage]:
    """What the code imports from outside, by (ecosystem, import name): the importing files and
    how many imports each makes (its imports only: not its calls or references)."""
    found: defaultdict[tuple[str, str], Usage] = defaultdict(Usage)
    outside = {NodeKind.PACKAGE.value, NodeKind.EXTERNAL_SYMBOL.value}
    for a, b, index in graph.graph.weighted_edge_list():
        target = graph.nodes[b]
        if target.kind not in outside or graph.edges[index].type is not EdgeType.IMPORTS:
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


def _top(ecosystem: str, imported: str) -> str:
    """What an import is listed as when nothing declares it: a module's top package."""
    return imported.split(".", 1)[0] if ecosystem == "pypi" else imported


def _candidates(ecosystem: str, imported: str) -> list[str]:
    """The distributions an import may be, most specific first: ``google.cloud.storage`` is
    ``google-cloud-storage`` before it is any ``google``."""
    if ecosystem == "npm":
        return [imported]
    parts = imported.split(".")
    dotted = [canonicalize_name("-".join(parts[:n])) for n in range(len(parts), 1, -1)]
    top = parts[0]
    return [*dotted, canonicalize_name(top), *IMPORT_NAMES.get(top.lower(), ())]


def _builtin(ecosystem: str, imported: str) -> bool:
    if ecosystem == "pypi":
        top = _top(ecosystem, imported)
        return top in sys.stdlib_module_names or top.startswith("_")
    # The resolver names Node's own modules "node:<name>" (and some code imports them so).
    return imported.startswith("node:") or imported in NODE_BUILTINS


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
    undeclared: dict[tuple[str, str], PackageInUse] = {}
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
            top = (ecosystem, _top(ecosystem, imported))
            held = undeclared.get(top)
            if held is None:
                held = undeclared[top] = PackageInUse(top[0], top[1], [], Usage())
            held.usage.files.update(counts.files)

    def rank(p: PackageInUse) -> tuple[int, int, str, str]:
        return (-len(p.usage.files), -p.usage.imports, p.ecosystem, p.name)

    return sorted(by_name.values(), key=rank), sorted(undeclared.values(), key=rank)
