"""How risky a fix is, and how ready to merge (8.7), each with its reasons.

**Risk** (:func:`assess`), from the files a fix changes (the plan's, then the patch's), what
depends on them in the graph, and what it says and does:

- **high** when it touches a sensitive area: auth (login, passwords, JWTs, permissions),
  payments (billing, invoices, prices, refunds), migrations (schema changes), or concurrency
  (locks, threads, transactions, races). An area is found by the changed files' paths, the
  changed symbols' names, the routes and tables that depend on them nearby in the graph (two
  hops), and the words of the root cause, the plan and the diff;
- high, too, when the bug wasn't reproduced, or the diff changes more than
  :data:`MAX_LINES` lines or :data:`MAX_FILES` files;
- **medium** when it changes more than :data:`MEDIUM_LINES` lines or :data:`MEDIUM_FILES`
  files, or the code it changes reaches several routes or a table;
- **low** otherwise.

**Merge readiness** (:func:`readiness`), 0 to 5: 5 for a reproduced bug whose fix passes its
test and the whole suite, low risk, nothing flagged; less for each thing a reviewer must look
at themselves (its risk, failures the suite had before, a verifier's concern, code no test
reaches, no extra tests or one that fails), each said.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from sensei_core.models import FixRisk
from sensei_engine.graph.snapshot import GraphSnapshot, Node
from sensei_engine.impact import changed_nodes, impact

MAX_LINES = 200
MAX_FILES = 5
MEDIUM_LINES = 50
MEDIUM_FILES = 2
MEDIUM_ROUTES = 3
NEAR = 2  # hops: what depends on the change this close says what it is
_WORDS = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+")
AREAS: dict[str, frozenset[str]] = {
    "auth": frozenset({
        "auth", "authn", "authz", "authenticate", "authentication", "authorize",
        "authorization", "login", "logout", "signin", "signup", "password", "passwords",
        "passwd", "oauth", "jwt", "permission", "permissions", "acl", "rbac", "sso", "saml",
        "oidc", "credential", "credentials", "mfa", "otp", "csrf", "privilege", "privileges",
    }),
    "payments": frozenset({
        "payment", "payments", "billing", "invoice", "invoices", "charge", "charges", "stripe",
        "checkout", "refund", "refunds", "pricing", "price", "prices", "ledger", "wallet",
        "payout", "payouts", "subscription", "subscriptions", "currency", "money", "tax",
        "taxes", "cents",
    }),
    "migrations": frozenset({"migration", "migrations", "alembic", "migrate"}),
    "concurrency": frozenset({
        "lock", "locks", "locking", "mutex", "semaphore", "threads", "threading",
        "concurrent", "concurrency", "race", "atomic", "atomically", "multiprocessing",
        "deadlock", "rlock",
    }),
}  # fmt: skip
# In code (a diff's lines): what concurrency looks like.
_CONCURRENCY_CODE = re.compile(
    r"\bthreading\b|\bmultiprocessing\b|asyncio\.(Lock|Semaphore|Condition|Event)\b|\bRLock\(|"
    r"\bLock\(\)|with_for_update|select_for_update|FOR UPDATE|transaction\.atomic|"
    r"\bAtomics\.|worker_threads|SharedArrayBuffer|\bMutex\b"
)
_MIGRATION_PATH = re.compile(r"(^|/)(migrations|alembic|migrate)/|(^|/)versions/\w+\.py$|"
                             r"\.sql$|(^|/)schema\.(prisma|rb|sql)$")  # fmt: skip


def words(text: str) -> set[str]:
    """``text``'s words, lowercased, split at case changes, ``_`` and punctuation."""
    return {w.lower() for w in _WORDS.findall(text)}


@dataclass(frozen=True, slots=True)
class Areas:
    """The sensitive areas a change touches, each with what showed it."""

    found: dict[str, str] = field(default_factory=dict[str, str])

    def add(self, area: str, why: str) -> None:
        self.found.setdefault(area, why)


def _area_of(found: set[str]) -> str | None:
    for area, vocabulary in AREAS.items():
        if found & vocabulary:
            return area
    return None


def path_areas(paths: Iterable[str], areas: Areas) -> None:
    for path in paths:
        if _MIGRATION_PATH.search(path):
            areas.add("migrations", f"{path} is a migration")
        for part in PurePosixPath(path).parts:
            area = _area_of(words(part))
            if area:
                areas.add(area, f"{path} is {area} code")


def text_areas(texts: Iterable[tuple[str, str]], areas: Areas) -> None:
    """Areas the words of ``texts`` (each ``(what, text)``: the root cause, the plan) name."""
    for what, text in texts:
        found = words(text)
        for area, vocabulary in AREAS.items():
            hits = sorted(found & vocabulary)
            if hits:
                areas.add(area, f"{what} says {hits[0]!r}")


def code_areas(diff_lines: Iterable[tuple[str, str]], areas: Areas) -> None:
    """Concurrency in the code a diff adds or removes (each ``(path, line)``)."""
    for path, line in diff_lines:
        if _CONCURRENCY_CODE.search(line):
            areas.add("concurrency", f"{path} changes {line.strip()[:80]!r}")


def graph_areas(
    graph: GraphSnapshot | None, files: Iterable[str], areas: Areas, *, lines: Mapping[str,
    tuple[int, int]] | None = None,
) -> dict[str, Any] | None:  # fmt: skip
    """Areas the graph shows for a change to ``files`` (to ``lines`` of them, when given): the
    changed symbols' names, and the routes and tables that depend on them within :data:`NEAR`
    hops. Its blast radius as kept (None: no graph)."""
    if graph is None:
        return None
    changed = [n for n in changed_nodes(graph, files=files) if _within(n, lines)]
    for node in changed:
        area = _area_of(words(node.name))
        if area:
            areas.add(area, f"it changes {node.qualified_name or node.name} ({node.path})")
    found = impact(graph, changed)
    for category in ("routes", "tables"):
        for item in found.affected.get(category, []):
            if item.distance > NEAR:
                continue
            area = _area_of(words(f"{item.node.name} {item.node.qualified_name}"))
            if area:
                areas.add(area, f"{item.node.name} depends on it")
    return {
        "changed": [n.symbol_key for n in changed[:20]],
        "counts": {name: len(items) for name, items in found.affected.items()},
        "top": {
            name: [{"key": a.node.symbol_key, "name": a.node.name, "path": a.node.path,
                    "score": round(a.score, 3), "distance": a.distance} for a in items[:5]]
            for name, items in found.affected.items() if items
        },
        "truncated": found.truncated,
    }  # fmt: skip


def _within(node: Node, lines: Mapping[str, tuple[int, int]] | None) -> bool:
    if lines is None or node.path is None or node.path not in lines:
        return True
    start, end = lines[node.path]
    return node.start_line <= end and start <= node.end_line


@dataclass(frozen=True, slots=True)
class Risk:
    level: FixRisk
    reasons: tuple[str, ...]

    def json(self) -> dict[str, Any]:
        return {"level": self.level.value, "reasons": list(self.reasons)}


_ORDER = {FixRisk.LOW: 0, FixRisk.MEDIUM: 1, FixRisk.HIGH: 2}


def higher(a: FixRisk, b: FixRisk) -> bool:
    """``a`` is riskier than ``b``."""
    return _ORDER[a] > _ORDER[b]


def assess(
    *,
    areas: Areas,
    reproduced: bool,
    files: int,
    lines: int | None,
    blast: Mapping[str, Any] | None,
) -> Risk:
    """The risk of a change to ``files`` files and ``lines`` lines (None: not written yet)."""
    high: list[str] = [f"touches {area}: {why}" for area, why in areas.found.items()]
    if not reproduced:
        high.append("the bug wasn't reproduced")
    if lines is not None and lines > MAX_LINES:
        high.append(f"{lines} changed lines (over {MAX_LINES})")
    if files > MAX_FILES:
        high.append(f"{files} files (over {MAX_FILES})")
    if high:
        return Risk(FixRisk.HIGH, tuple(high))
    medium: list[str] = []
    if lines is not None and lines > MEDIUM_LINES:
        medium.append(f"{lines} changed lines (over {MEDIUM_LINES})")
    if files > MEDIUM_FILES:
        medium.append(f"{files} files (over {MEDIUM_FILES})")
    counts: Mapping[str, int] = (blast or {}).get("counts") or {}
    if counts.get("routes", 0) >= MEDIUM_ROUTES:
        medium.append(f"{counts['routes']} routes depend on it")
    if counts.get("tables", 0):
        medium.append(f"it reaches {counts['tables']} table(s)")
    if medium:
        return Risk(FixRisk.MEDIUM, tuple(medium))
    size = f"{lines} changed lines" if lines is not None else "a small change"
    return Risk(FixRisk.LOW, (f"{files} file(s), {size}, no sensitive area",))


@dataclass(frozen=True, slots=True)
class Readiness:
    score: int
    reason: str

    def json(self) -> dict[str, Any]:
        return {"score": self.score, "reason": self.reason}


def readiness(
    *,
    risk: FixRisk,
    known_failures: int,
    concerns: Sequence[str],
    covered: bool | None,
    extra_failed: bool,
    extra_written: bool,
) -> Readiness:
    """How ready a verified fix is to merge (see the module)."""
    score = 5
    said: list[str] = []
    if risk is FixRisk.HIGH:
        score -= 2
        said.append("high risk")
    elif risk is FixRisk.MEDIUM:
        score -= 1
        said.append("medium risk")
    if known_failures:
        score -= 1
        said.append(f"{known_failures} test(s) failed before the fix too (not verified)")
    if concerns:
        score -= 1
        said.append(f"the verifier's concern: {concerns[0][:160]}")
    if covered is False:
        score -= 1
        said.append("no existing test reaches the root cause's code")
    if not extra_written:
        score -= 1
        said.append("no extra tests on the changed lines")
    if extra_failed:
        score = min(score, 1)
        said.append("an extra test on the changed lines fails")
    score = max(0, score)
    if not said:
        return Readiness(5, "Reproduced; the fix passes its test, the whole suite and extra "
                         "tests on the changed lines; low risk; nothing flagged.")  # fmt: skip
    return Readiness(score, "; ".join(said).capitalize() + ".")
