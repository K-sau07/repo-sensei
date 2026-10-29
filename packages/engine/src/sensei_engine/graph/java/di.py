"""Spring's dependency injection as graph edges (3.18): ``INJECTS`` from a bean to each type it
is given.

A **bean** is a class with a stereotype (``@Component``, ``@Service``, ``@Repository``,
``@Controller``, ``@RestController``, ``@Configuration``, ...; JSR-330's ``@Named`` too). What it
is given:

- a field with ``@Autowired``, ``@Inject`` or ``@Resource`` (in any class: field injection);
- the parameters of its constructor: the ``@Autowired``/``@Inject`` one, else its only one
  (each one's field, when the constructor assigns it: ``this.repo = repo``);
- Lombok's constructors: ``@RequiredArgsConstructor``, the final fields (and ``@NonNull`` ones)
  with no initializer; ``@AllArgsConstructor``, every instance field with none;
- the parameters of an ``@Autowired`` method (a setter), and of a ``@Bean`` method (the edge
  goes from the method: what it makes is given them).

A parameter with ``@Value`` is configuration, not a bean, and a primitive or a ``String`` is
never one. A collection of beans (``List<Handler>``, ``Optional<T>``, ``ObjectProvider<T>``,
``Map<String, T>``) injects its element type. The type is named where it is declared (as
:mod:`.imports` resolves names): one of the repository's types, or an external's node. Edges are
``import_resolved``, at the field's or parameter's line, with how (``attrs.via``: field,
constructor, lombok, setter, bean) and the name. An interface's edge is to the interface (the
trace reaches its implementations by their ``IMPLEMENTS`` edges); when exactly one bean of the
repository implements it (or one of several is ``@Primary``), Spring gives that one: a second
edge goes to it, ``receiver_heuristic`` (a bean made elsewhere, by a ``@Bean`` method or a
library, isn't seen), ``attrs.implements`` the interface.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from sensei_core.models import EdgeType, Provenance
from sensei_engine.graph.java.facts import Annotation, JavaType, Method, Variable
from sensei_engine.graph.java.imports import (
    PRIMITIVES,
    Declared,
    TypeIndex,
    TypeRef,
    base_name,
    external_node,
    is_array,
    type_arguments,
)
from sensei_engine.graph.writer import GraphEdge, GraphNode

STEREOTYPES = frozenset({
    "Component", "Service", "Repository", "Controller", "RestController", "Configuration",
    "ControllerAdvice", "RestControllerAdvice", "SpringBootApplication", "Named", "Singleton",
    "ApplicationScoped", "RequestScoped", "Endpoint", "Indexed",
})  # fmt: skip
INJECTED = frozenset({"Autowired", "Inject", "Resource"})
CONTAINERS = frozenset({
    "List", "Set", "Collection", "Iterable", "Optional", "ObjectProvider", "Provider",
    "ObjectFactory", "Map", "SortedSet", "SortedMap",
})  # fmt: skip
NOT_BEANS = frozenset({"String", "Integer", "Long", "Boolean", "Double", "Float", "Duration"})

Via = Literal["field", "constructor", "lombok", "setter", "bean"]


@dataclass(frozen=True, slots=True)
class Injection:
    via: Via
    name: str  # the field's or parameter's
    type: str  # as written
    line: int
    source: str  # the bean's key (a @Bean method's, for its parameters)
    field: str | None = None  # the field a constructor's parameter is assigned to


@dataclass(slots=True)
class DiStats:
    beans: int = 0  # classes with a stereotype
    injections: int = 0  # injection points found
    injects: int = 0  # edges to the injected types
    implementations: int = 0  # ... and to the one bean implementing an injected interface
    unresolved: list[tuple[str, int, str]] = field(default_factory=list[tuple[str, int, str]])


def names(annotations: tuple[Annotation, ...]) -> set[str]:
    """Annotations' simple names (``org.springframework.stereotype.Service``: ``Service``)."""
    return {a.name.rsplit(".", 1)[-1] for a in annotations}


def stereotype(declared: JavaType) -> str | None:
    return next((n for n in sorted(names(declared.annotations)) if n in STEREOTYPES), None)


def injections(declared: JavaType) -> list[Injection]:
    """What a type is given (see the module), in source order."""
    found: list[Injection] = []
    for variable in declared.fields:
        if not variable.static and names(variable.annotations) & INJECTED:
            found.append(_of(variable, "field", declared.key))
    if declared.kind == "class" and stereotype(declared) is not None:
        found.extend(_constructed(declared))
    for method in declared.methods:
        marks = names(method.annotations)
        if not method.constructor and marks & INJECTED:
            found.extend(_parameters(method, "setter", declared.key))
        elif "Bean" in marks:
            found.extend(_parameters(method, "bean", method.key))
    return sorted(found, key=lambda i: (i.line, i.name))


def _of(variable: Variable, via: Via, source: str, assigned: str | None = None) -> Injection:
    return Injection(via, variable.name, variable.type or "", variable.line, source, assigned)


def _parameters(method: Method, via: Via, source: str) -> Iterator[Injection]:
    assigned = {parameter: name for name, parameter in method.assigns}
    for parameter in method.parameters:
        if "Value" not in names(parameter.annotations):
            yield _of(parameter, via, source, assigned.get(parameter.name))


def _constructed(declared: JavaType) -> list[Injection]:
    """A bean's constructor's parameters: its own constructor's, or Lombok's."""
    constructors = [m for m in declared.methods if m.constructor]
    chosen = [m for m in constructors if names(m.annotations) & INJECTED]
    if not chosen and len(constructors) == 1:
        chosen = constructors
    if chosen:
        return list(_parameters(chosen[0], "constructor", declared.key))
    marks = names(declared.annotations)
    instance = [f for f in declared.fields if not f.static and f.key is not None]
    if "RequiredArgsConstructor" in marks:
        given = [
            f for f in instance
            if not f.initialized and (f.final or "NonNull" in names(f.annotations))
        ]  # fmt: skip
    elif "AllArgsConstructor" in marks:
        given = [f for f in instance if not f.initialized]
    else:
        return []
    return [_of(f, "lombok", declared.key, f.name) for f in given]


def bean_type(written: str) -> str | None:
    """The type a bean of type ``written`` injects: a collection's element, else itself (None:
    not a bean)."""
    name = base_name(written)
    if not name or name in PRIMITIVES or is_array(written):
        return None
    if name.rsplit(".", 1)[-1] in CONTAINERS:
        arguments = type_arguments(written)
        return bean_type(arguments[-1]) if arguments else None
    return None if name in NOT_BEANS else written


def bean_classes(index: TypeIndex) -> list[Declared]:
    """The repository's beans: its classes with a stereotype."""
    return [
        d
        for _, d in sorted(index.by_key.items())
        if d.type.kind == "class" and stereotype(d.type) is not None
    ]


def implementation(index: TypeIndex, beans: list[Declared], ref: TypeRef) -> str | None:
    """The bean Spring gives for an interface (or a class that isn't one itself): the one bean
    of its type, or the one of several marked ``@Primary`` (None: no bean, or several)."""
    if ref.external or any(b.type.key == ref.key for b in beans):
        return None
    found = [b for b in beans if any(r.key == ref.key for r in index.hierarchy(b.ref()))]
    if len(found) > 1:
        found = [b for b in found if "Primary" in names(b.type.annotations)]
    return found[0].type.key if len(found) == 1 else None


def resolve(
    index: TypeIndex,
    path: str,
    stats: DiStats,
    nodes: dict[str, GraphNode],
    beans: list[Declared] | None = None,
) -> tuple[list[GraphEdge], dict[str, list[dict[str, str]]]]:
    """A file's ``INJECTS`` edges, and each bean's injections (for its node's attrs)."""
    beans = bean_classes(index) if beans is None else beans
    edges: list[GraphEdge] = []
    kept: dict[str, list[dict[str, str]]] = {}
    for declared in index.facts[path].types:
        entry = index.by_key.get(declared.key)
        if entry is None:
            continue
        if declared.kind == "class" and stereotype(declared) is not None:
            stats.beans += 1
        for injection in injections(declared):
            stats.injections += 1
            ref = _target(index, entry, injection)
            if ref is None:
                if bean_type(injection.type) is not None:
                    stats.unresolved.append((path, injection.line, injection.type))
                continue
            if ref.external:
                nodes.setdefault(ref.key, external_node(ref.key))
            attrs = {"via": injection.via, "name": injection.name}
            if injection.field:
                attrs["field"] = injection.field
            edges.append(
                GraphEdge(
                    injection.source,
                    ref.key,
                    EdgeType.INJECTS,
                    Provenance.IMPORT_RESOLVED,
                    evidence_path=path,
                    evidence_line=injection.line,
                    attrs=attrs,
                    owner=path,
                )
            )
            stats.injects += 1
            bean = implementation(index, beans, ref)
            if bean is not None:  # an interface's one implementation: the bean given
                stats.implementations += 1
                edges.append(
                    GraphEdge(
                        injection.source,
                        bean,
                        EdgeType.INJECTS,
                        Provenance.RECEIVER_HEURISTIC,
                        evidence_path=path,
                        evidence_line=injection.line,
                        attrs={**attrs, "implements": ref.key},
                        owner=path,
                    )
                )
                attrs = {**attrs, "bean": bean}
            kept.setdefault(injection.source, []).append({**attrs, "type": ref.key})
    return edges, kept


def _target(index: TypeIndex, entry: Declared, injection: Injection) -> TypeRef | None:
    written = bean_type(injection.type)
    if written is None:
        return None
    return index.resolve(entry.path, entry.type.key, written)
