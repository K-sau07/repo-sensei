"""Java imports and type names (3.18): which type each name a file writes means, and the
``IMPORTS``, ``INHERITS`` and ``IMPLEMENTS`` edges.

**Packages.** Every file says its package (``package com.acme.shop.web;``), so the package of
each type is known without guessing source roots (``src/main/java``, a Gradle module's, a
generated one): a type's fully qualified name is its file's package and its name, a nested
type's its outer type's and its own (``com.acme.shop.domain.Order.Line``). A type declared in
a callable (a local class) has none.

**Names** (:meth:`TypeIndex.resolve`) are looked up as the compiler does: the type a name is
written in, the types around it and their supertypes' member types, the file's own types,
single-type imports (``import a.b.C``, ``import a.b.Outer.Inner``), the package's types, then
on-demand imports (``import a.b.*``) with ``java.lang`` among them. A qualified name is a type
and its member types (``Order.Line``), or a fully qualified one (``java.util.List``). What no
file of the repository declares is an external: a node ``external_symbol java:<FQN>`` (its
methods ``java:<FQN>.<name>``), its FQN the import's. A name only an on-demand import of a
library can give is that import's when there is one such import, or it is a well-known one
(``java.util``'s, ...); else it is left unknown (and nothing is guessed through it).

**Edges.** Each import gives an ``IMPORTS`` edge from the file's module (``import_resolved``):
to the type (or the external) it names; an on-demand import of the repository's package to each
of its types the file uses, of a library's to the package's node (``package java:java.util``);
a static import to the members it names (every overload) or the class. A type gets an
``INHERITS`` edge to what it extends and an ``IMPLEMENTS`` edge to what it implements, class to
class (an interface extending another inherits it), in the repository or out; the trace steps
from an interface's method to its implementations along them.

An import is **internal** when its package (or class) is one the repository declares; it is
**resolved** when its target is found, else **unresolved** (a generated class, a file left
out); the rest are **external**.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.java.facts import JavaFacts, JavaImport, JavaType
from sensei_engine.graph.writer import GraphEdge, GraphNode

MAX_NESTING = 32  # supertypes resolved inside the resolving of others' (see supertypes)
PRIMITIVES = frozenset({
    "boolean", "byte", "char", "short", "int", "long", "float", "double", "void",
})  # fmt: skip
# java.lang's types a program names without importing them (the common ones).
JAVA_LANG = frozenset({
    "AutoCloseable", "Boolean", "Byte", "CharSequence", "Character", "Class", "ClassCastException",
    "ClassLoader", "CloneNotSupportedException", "Cloneable", "Comparable", "Deprecated", "Double",
    "Enum", "Error", "Exception", "Float", "FunctionalInterface", "IllegalArgumentException",
    "IllegalStateException", "IndexOutOfBoundsException", "Integer", "InterruptedException",
    "Iterable", "Long", "Math", "NullPointerException", "Number", "NumberFormatException",
    "Object", "Override", "Process", "Record", "Runnable", "Runtime", "RuntimeException",
    "SafeVarargs", "SecurityException", "Short", "StackOverflowError", "StrictMath", "String",
    "StringBuffer", "StringBuilder", "SuppressWarnings", "System", "Thread", "ThreadLocal",
    "Throwable", "UnsupportedOperationException", "Void", "ArithmeticException",
    "ArrayIndexOutOfBoundsException", "ArrayStoreException", "AssertionError",
    "NoSuchFieldException", "NoSuchMethodException", "ReflectiveOperationException",
    "OutOfMemoryError",
})  # fmt: skip
# Well-known types of packages often imported on demand together (``import java.util.*;``
# with another): the package a name comes from when several on-demand imports could give it.
WELL_KNOWN: Mapping[str, frozenset[str]] = {
    "java.util": frozenset({
        "ArrayList", "Arrays", "Collection", "Collections", "Comparator", "Date", "Deque",
        "EnumMap", "EnumSet", "HashMap", "HashSet", "Iterator", "LinkedHashMap", "LinkedHashSet",
        "LinkedList", "List", "Locale", "Map", "NoSuchElementException", "Objects", "Optional",
        "Properties", "Queue", "Random", "Set", "SortedMap", "SortedSet", "Stack", "TreeMap",
        "TreeSet", "UUID", "Vector",
    }),
    "java.util.stream": frozenset({"Collectors", "Stream", "IntStream", "LongStream"}),
    "java.util.function": frozenset({
        "BiConsumer", "BiFunction", "Consumer", "Function", "Predicate", "Supplier",
        "UnaryOperator",
    }),
    "java.io": frozenset({
        "File", "IOException", "InputStream", "OutputStream", "Serializable",
        "UncheckedIOException",
    }),
    "java.time": frozenset({
        "Duration", "Instant", "LocalDate", "LocalDateTime", "LocalTime", "OffsetDateTime",
        "ZoneId", "ZonedDateTime",
    }),
}  # fmt: skip


def base_name(written: str) -> str:
    """A type as written without its type arguments, annotations, bounds or array brackets:
    ``Map.Entry<K, V>[]`` is ``Map.Entry``, ``? extends Order`` is ``Order``."""
    text = written.split("<", 1)[0]
    words = [w for w in text.replace("[]", " ").split() if not w.startswith("@")]
    if words[:1] == ["?"]:
        words = words[2:] if len(words) > 2 and words[1] in ("extends", "super") else []
    words = [w for w in words if w not in ("final",)]
    return words[-1].rstrip(".") if words else ""


def type_arguments(written: str) -> list[str]:
    """The type arguments a type is written with: ``JpaRepository<Order, Long>`` has
    ``["Order", "Long"]`` (nested ones kept whole: ``Map<String, List<Order>>``)."""
    start = written.find("<")
    if start < 0 or not written.rstrip().endswith(">"):
        return []
    inner = written[start + 1 : written.rstrip().rfind(">")]
    found: list[str] = []
    depth, current = 0, ""
    for char in inner:
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        if char == "," and depth == 0:
            found.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        found.append(current.strip())
    return found


def is_array(written: str) -> bool:
    return written.rstrip().endswith("]") or "..." in written


@dataclass(frozen=True, slots=True)
class TypeRef:
    """What a type name means: one of the repository's types (``key`` its symbol key) or an
    external (``key`` its node's, ``external_symbol java:<fqn>``)."""

    key: str
    fqn: str
    external: bool = False
    path: str | None = None  # the file a repository's type is in


def external_key(fqn: str) -> str:
    return f"external_symbol java:{fqn}"


def package_key(package: str) -> str:
    return f"package java:{package}"


def external(fqn: str) -> TypeRef:
    return TypeRef(external_key(fqn), fqn, external=True)


def external_node(key: str) -> GraphNode:
    """The node of an external: ``external_symbol java:<fqn>`` (a type, or a type's member:
    ``java:org.springframework.data.repository.CrudRepository.save``) or ``package java:<p>``."""
    kind, _, rest = key.partition(" ")
    name = rest.removeprefix("java:")
    if kind == "package":
        return GraphNode(key, NodeKind.PACKAGE, name, lang="java", attrs={"package": name})
    parts = name.split(".")
    # The package: the parts before the first capitalized one (a type's, by convention).
    first_type = next((i for i, p in enumerate(parts) if p[:1].isupper()), len(parts) - 1)
    attrs = {"package": ".".join(parts[:first_type]), "fqn": name}
    return GraphNode(
        key,
        NodeKind.EXTERNAL_SYMBOL,
        parts[-1],
        qualified_name=name,
        lang="java",
        attrs=attrs,
    )


@dataclass(frozen=True, slots=True)
class Declared:
    """One of the repository's types, where it is."""

    path: str
    type: JavaType
    fqn: str | None  # None: a local class
    facts: JavaFacts

    def ref(self) -> TypeRef:
        return TypeRef(self.type.key, self.fqn or self.type.qualified, path=self.path)


@dataclass(slots=True)
class ImportStats:
    internal: int = 0  # imports of the repository's packages and types
    resolved: int = 0  # ... found
    external: int = 0  # of libraries'
    unresolved: list[tuple[str, int, str]] = field(default_factory=list[tuple[str, int, str]])

    @property
    def rate(self) -> float:
        return self.resolved / self.internal if self.internal else 1.0


@dataclass(slots=True)
class CodeStats:
    types: int = 0  # classes, interfaces, enums, records, annotation types
    inherits: int = 0  # INHERITS edges to the repository's types
    implements: int = 0  # IMPLEMENTS edges to the repository's types
    external_bases: int = 0  # either, to a library's types
    annotated: int = 0  # definitions given attrs["java"]


class TypeIndex:
    """The repository's Java types by FQN and key, and how a file's names resolve to them
    (see the module)."""

    def __init__(self, facts: Mapping[str, JavaFacts]) -> None:
        self.facts = facts
        self.by_fqn: dict[str, Declared] = {}  # the first by path of an FQN declared twice
        self.twins: dict[str, list[Declared]] = {}  # an FQN's declarations, when several
        self._near = ""  # the file a name is being resolved in (see :meth:`declared`)
        self.by_key: dict[str, Declared] = {}
        self.packages: dict[str, dict[str, str]] = {}  # package: simple name -> FQN
        self.members: dict[str, dict[str, str]] = {}  # a type's key: member type name -> key
        for path in sorted(facts):
            file = facts[path]
            keys = {t.key: t for t in file.types}
            for declared in file.types:
                fqn = self._fqn(file, declared, keys)
                entry = Declared(path, declared, fqn, file)
                self.by_key.setdefault(declared.key, entry)
                if fqn is not None:
                    if fqn in self.by_fqn:  # two modules' classes of one name
                        self.twins.setdefault(fqn, [self.by_fqn[fqn]]).append(entry)
                    self.by_fqn.setdefault(fqn, entry)
                if declared.outer is not None and not declared.local:
                    self.members.setdefault(declared.outer, {})[declared.name] = declared.key
                if declared.outer is None and not declared.local:
                    self.packages.setdefault(file.package, {}).setdefault(declared.name, fqn or "")
        self._supertypes: dict[str, list[tuple[str, TypeRef | None, str]]] = {}
        self._resolving: set[str] = set()
        self._hierarchies: dict[str, list[TypeRef]] = {}

    @staticmethod
    def _fqn(file: JavaFacts, declared: JavaType, keys: Mapping[str, JavaType]) -> str | None:
        if declared.local:
            return None
        names = [declared.name]
        outer = declared.outer
        while outer is not None:
            held = keys.get(outer)
            if held is None or held.local:
                return None
            names.append(held.name)
            outer = held.outer
        dotted = ".".join(reversed(names))
        return f"{file.package}.{dotted}" if file.package else dotted

    def internal(self, dotted: str) -> bool:
        """Whether a package (or a type) is the repository's."""
        return dotted in self.packages or dotted in self.by_fqn

    # ------------------------------------------------------------------ names

    def resolve(self, path: str, owner: str | None, written: str) -> TypeRef | None:
        """What a type as written in file ``path`` (inside type ``owner``) means; None: a
        primitive, an array, a type variable or an external we can't name."""
        around, self._near = self._near, path
        try:
            return self._resolve(path, owner, written)
        finally:
            self._near = around

    def declared(self, fqn: str) -> Declared | None:
        """The repository's type of an FQN; of two modules' of one name, the one nearest the
        file being resolved (the longest path they share: its own module's)."""
        twins = self.twins.get(fqn)
        if not twins:
            return self.by_fqn.get(fqn)
        return max(twins, key=lambda d: len(commonpath_prefix(d.path, self._near)))

    def _resolve(self, path: str, owner: str | None, written: str) -> TypeRef | None:
        if not written or is_array(written):
            return None
        name = base_name(written)
        if not name or name in PRIMITIVES or name == "var":
            return None
        parts = name.split(".")
        first = self._simple(path, owner, parts[0])
        if first is None:
            return self._qualified(parts)
        for part in parts[1:]:
            first = self.member_type(first, part)
            if first is None:
                return None
        return first

    def is_type_variable(self, path: str, owner: str | None, name: str, method: str | None) -> bool:
        """Whether a name is a type parameter of the type (or one around it) or the method."""
        declared = self.by_key.get(owner or "")
        while declared is not None:
            if name in declared.type.type_parameters:
                return True
            if method is not None and any(
                name in m.type_parameters for m in declared.type.methods if m.key == method
            ):
                return True
            declared = self.by_key.get(declared.type.outer or "")
        return False

    def _simple(self, path: str, owner: str | None, name: str) -> TypeRef | None:
        file = self.facts.get(path)
        if file is None:
            return None
        found = self._around(owner, name)
        if found is not None:
            return found
        for declared in file.types:  # the file's own (top-level) types
            if declared.outer is None and declared.name == name and not declared.local:
                return self.by_key[declared.key].ref()
        found = self._imported(file, name)
        if found is not None:
            return found
        fqn = self.packages.get(file.package, {}).get(name)
        held = self.declared(fqn) if fqn else None
        if held is not None:
            return held.ref()
        return self._on_demand(file, name)

    def _around(self, owner: str | None, name: str) -> TypeRef | None:
        """The type itself, a type around it, or a member type of one of them (or of their
        supertypes)."""
        declared = self.by_key.get(owner or "")
        hops = 0
        while declared is not None and hops < 16:
            if declared.type.name == name:
                return declared.ref()
            member = self.member_type(declared.ref(), name)
            if member is not None:
                return member
            declared = self.by_key.get(declared.type.outer or "")
            hops += 1
        return None

    def member_type(self, ref: TypeRef, name: str) -> TypeRef | None:
        """A type's member type (its own, or a supertype's: ``Map.Entry``, ``Outer.Inner``)."""
        if ref.external:
            return external(f"{ref.fqn}.{name}")
        for current in self.hierarchy(ref):
            if current.external:
                continue
            key = self.members.get(current.key, {}).get(name)
            if key is not None:
                return self.by_key[key].ref()
        return None

    def _imported(self, file: JavaFacts, name: str) -> TypeRef | None:
        for item in file.imports:
            if item.kind == "single" and item.name == name:
                return self.fqn(item.target)
        return None

    def _on_demand(self, file: JavaFacts, name: str) -> TypeRef | None:
        libraries: list[str] = []
        for item in file.imports:
            if item.kind != "wildcard":
                continue
            inner = self.in_package(item.source, name)
            if inner is not None:
                return inner
            if not self.internal(item.source):
                libraries.append(item.source)
        if name in JAVA_LANG:
            return external(f"java.lang.{name}")
        known = [p for p in libraries if name in WELL_KNOWN.get(p, ())]
        if len(known) == 1:
            return external(f"{known[0]}.{name}")
        if (
            len(libraries) == 1
            and name[:1].isupper()  # a type's name, by convention (not a misread variable)
            and not any(name in names for names in WELL_KNOWN.values())
        ):
            return external(f"{libraries[0]}.{name}")
        return None

    def in_package(self, source: str, name: str) -> TypeRef | None:
        """``name`` in an on-demand import's package, or as a member of its type."""
        fqn = self.packages.get(source, {}).get(name)
        held = self.declared(fqn) if fqn else None
        if held is not None:
            return held.ref()
        declared = self.declared(source)
        if declared is not None:
            return self.member_type(declared.ref(), name)
        return None

    def _qualified(self, parts: list[str]) -> TypeRef | None:
        """A fully qualified name (``com.acme.shop.domain.Order.Line``, ``java.util.List``)."""
        for end in range(len(parts), 0, -1):
            declared = self.declared(".".join(parts[:end]))
            if declared is None:
                continue
            found: TypeRef | None = declared.ref()
            for part in parts[end:]:
                found = self.member_type(found, part) if found is not None else None
            return found
        if len(parts) > 1 and parts[0][:1].islower() and parts[-1][:1].isupper():
            return external(".".join(parts))
        return None

    def fqn(self, dotted: str) -> TypeRef:
        """A fully qualified type: the repository's, else an external."""
        found = self._qualified(dotted.split("."))
        return found if found is not None else external(dotted)

    # ------------------------------------------------------------------ supertypes

    def supertypes(self, key: str) -> list[tuple[str, TypeRef | None, str]]:
        """What one of the repository's types extends and implements: (``extends`` or
        ``implements``, what it resolves to, as written)."""
        held = self._supertypes.get(key)
        if held is not None:
            return held
        declared = self.by_key.get(key)
        if declared is None or key in self._resolving:
            return []
        if len(self._resolving) >= MAX_NESTING:
            # A supertype named through another type's member (class V1 extends V2.W) asks for
            # V2's hierarchy, and so on: past this depth the answer is none, and not kept (a
            # type asked for itself gets its own).
            return []
        self._resolving.add(key)
        # A type's supertypes are named in the scope around it (its own member types too).
        found: list[tuple[str, TypeRef | None, str]] = []
        for relation, names in (
            ("extends", declared.type.extends),
            ("implements", declared.type.implements),
        ):
            for written in names:
                ref = self.resolve(declared.path, declared.type.outer or key, written)
                if ref is not None and ref.key == key:
                    ref = None
                found.append((relation, ref, written))
        self._resolving.discard(key)
        self._supertypes[key] = found
        return found

    def hierarchy(self, ref: TypeRef, *, skip_self: bool = False) -> list[TypeRef]:
        """A type and its supertypes, nearest first (breadth first; externals where they are
        reached, and not followed)."""
        held = self._hierarchies.get(ref.key)
        if held is None:
            held = self._walk(ref)
            if not self._resolving:  # complete: no supertypes left half-read
                self._hierarchies[ref.key] = held
        return held[1:] if skip_self else held

    def _walk(self, ref: TypeRef) -> list[TypeRef]:
        found: list[TypeRef] = []
        seen: set[str] = set()
        queue = [ref]
        while queue and len(found) < 64:
            current = queue.pop(0)
            if current.key in seen:
                continue
            seen.add(current.key)
            found.append(current)
            if not current.external:
                queue.extend(r for _, r, _ in self.supertypes(current.key) if r is not None)
        return found

    def superclass(self, key: str) -> TypeRef | None:
        """What a class extends (None: Object, or a name we can't resolve)."""
        return next((r for rel, r, _ in self.supertypes(key) if rel == "extends"), None)


def commonpath_prefix(a: str, b: str) -> str:
    """The leading path segments two paths share (``app/src/x.java``, ``app/lib/y.java`` →
    ``app/``)."""
    shared: list[str] = []
    for one, other in zip(a.split("/"), b.split("/"), strict=False):
        if one != other:
            break
        shared.append(one + "/")
    return "".join(shared)


# ---------------------------------------------------------------------- edges


def import_edges(
    index: TypeIndex, path: str, stats: ImportStats, nodes: dict[str, GraphNode]
) -> tuple[list[GraphEdge], dict[str, str]]:
    """A file's ``IMPORTS`` edges and its import table (each name an import binds: the key it
    means)."""
    file = index.facts[path]
    edges: list[GraphEdge] = []
    table: dict[str, str] = {}
    for item in file.imports:
        targets = _import_targets(index, file, item, stats, path)
        for target in targets:
            if target.startswith(("external_symbol ", "package ")):
                nodes.setdefault(target, external_node(target))
            edges.append(
                GraphEdge(
                    file.module,
                    target,
                    EdgeType.IMPORTS,
                    Provenance.IMPORT_RESOLVED,
                    evidence_path=path,
                    evidence_line=item.line,
                    attrs={"static": True} if item.kind.startswith("static") else {},
                    owner=path,
                )
            )
        if item.name != "*" and targets:
            table[item.name] = targets[0]
    return edges, table


def _import_targets(
    index: TypeIndex, file: JavaFacts, item: JavaImport, stats: ImportStats, path: str
) -> list[str]:
    internal = index.internal(item.source)
    targets: list[str] = []
    if item.kind == "single":
        found = index.fqn(item.target)
        targets = [found.key] if not (internal and found.external) else []
    elif item.kind == "wildcard":
        if internal:  # the package's (or the type's members') types the file uses
            used = sorted(file.type_names)
            targets = [t.key for n in used if (t := index.in_package(item.source, n)) is not None]
            targets = list(dict.fromkeys(targets))
        else:
            targets = [package_key(item.source)]
    else:
        targets = _static_targets(index, item, internal)
    if internal:
        stats.internal += 1
        if targets or item.kind == "wildcard":
            stats.resolved += 1
        else:
            stats.unresolved.append((path, item.line, item.target))
    else:
        stats.external += 1
    return targets


def _static_targets(index: TypeIndex, item: JavaImport, internal: bool) -> list[str]:
    """What a static import names: the members (every overload) or the class."""
    declared = index.by_fqn.get(item.source)
    if declared is None:
        if internal:
            return []
        return [external_key(item.target if item.name != "*" else item.source)]
    if item.name == "*":
        return [declared.type.key]
    members = [m.key for m in declared.type.methods if m.name == item.name]
    members += [f.key for f in declared.type.fields if f.name == item.name and f.key]
    member_type = index.members.get(declared.type.key, {}).get(item.name)
    if member_type is not None:
        members.append(member_type)
    return members or [declared.type.key]


def base_edges(
    index: TypeIndex, path: str, stats: CodeStats, nodes: dict[str, GraphNode]
) -> list[GraphEdge]:
    """Each type's ``INHERITS`` and ``IMPLEMENTS`` edges (see the module)."""
    edges: list[GraphEdge] = []
    for declared in index.facts[path].types:
        for relation, ref, written in index.supertypes(declared.key):
            if ref is None:
                continue
            kind = EdgeType.IMPLEMENTS if relation == "implements" else EdgeType.INHERITS
            if ref.external:
                nodes.setdefault(ref.key, external_node(ref.key))
                stats.external_bases += 1
            elif kind is EdgeType.IMPLEMENTS:
                stats.implements += 1
            else:
                stats.inherits += 1
            edges.append(
                GraphEdge(
                    declared.key,
                    ref.key,
                    kind,
                    Provenance.IMPORT_RESOLVED,
                    evidence_path=path,
                    evidence_line=declared.line,
                    attrs={"written": written} if written != ref.fqn.rsplit(".", 1)[-1] else {},
                    owner=path,
                )
            )
    return edges


def resolved_arguments(
    index: TypeIndex, path: str, owner: str | None, written: str
) -> list[dict[str, str]]:
    """A type's type arguments, each as written and what it resolves to (for node attrs)."""
    found: list[dict[str, str]] = []
    for argument in type_arguments(written):
        ref = index.resolve(path, owner, argument)
        entry = {"written": argument}
        if ref is not None:
            entry["target"] = ref.key
            entry["fqn"] = ref.fqn
        found.append(entry)
    return found
