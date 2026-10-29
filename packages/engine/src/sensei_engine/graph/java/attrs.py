"""What the Java resolver keeps on the definitions' nodes (``attrs["java"]``, merged in by the
graph stage), for the stages after it: Spring's routes (3.19) and JPA's entities and
repositories (3.20) read these rather than the source.

- a **type**: its ``fqn`` and ``kind``; its ``annotations``; its ``bases``, each as written with
  its relation (extends, implements), what it resolves to (``target``: a key of the repository's
  or an external's, with its ``fqn``) and its type arguments resolved the same way
  (``JpaRepository<Order, Long>``: ``Order``'s key and ``java.lang.Long``); ``repository``, for
  one extending a Spring Data repository: the ``entity`` and ``id`` type arguments; ``bean``, the
  stereotype that makes it one; ``injects``, what Spring gives it (:mod:`.di`);
- a **method** or constructor: its ``annotations``, its ``parameters`` (name, type as written,
  the type's target when it resolves, annotations: ``@PathVariable``, ``@RequestBody``), its
  ``returns`` (and ``returns_target``), ``static``;
- a **field**: its ``type`` as written, its ``target`` (the element type of a collection:
  ``List<OrderLine>``'s ``OrderLine``), its type ``arguments`` and ``annotations``
  (``@Id``, ``@Column(name = "...")``, ``@OneToMany(mappedBy = "...")``).

An annotation is ``{"name", "fqn", "arguments", "line"}``: its name as written, the FQN the
file's imports give it (an annotation of the repository's: its key as ``target``), its argument
list's source (``("/{id}")``, absent for a marker annotation).
"""

from __future__ import annotations

from typing import Any

from sensei_engine.graph.java.di import bean_type, stereotype
from sensei_engine.graph.java.facts import Annotation, Method, Variable
from sensei_engine.graph.java.imports import Declared, TypeIndex, resolved_arguments

# Spring Data's repository interfaces: a type extending one is a repository of its first type
# argument (the entity), keyed by its second (the id).
REPOSITORIES = frozenset({
    "Repository", "CrudRepository", "ListCrudRepository", "PagingAndSortingRepository",
    "ListPagingAndSortingRepository", "JpaRepository", "MongoRepository", "R2dbcRepository",
    "ReactiveCrudRepository", "ReactiveSortingRepository", "ElasticsearchRepository",
    "Neo4jRepository", "CassandraRepository", "RevisionRepository", "QuerydslPredicateExecutor",
    "JpaSpecificationExecutor",
})  # fmt: skip


def type_attrs(
    index: TypeIndex, declared: Declared, injects: list[dict[str, str]] | None
) -> dict[str, Any]:
    kind = declared.type
    attrs: dict[str, Any] = {"fqn": declared.fqn or kind.qualified, "kind": kind.kind}
    annotations = _annotations(index, declared, kind.key, kind.annotations)
    if annotations:
        attrs["annotations"] = annotations
    bases = _bases(index, declared)
    if bases:
        attrs["bases"] = bases
        repository = _repository(bases)
        if repository is not None:
            attrs["repository"] = repository
    bean = stereotype(kind) if kind.kind == "class" else None
    if bean is not None:
        attrs["bean"] = bean
    if injects:
        attrs["injects"] = injects
    return attrs


def _bases(index: TypeIndex, declared: Declared) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    owner = declared.type.outer or declared.type.key
    for relation, ref, written in index.supertypes(declared.type.key):
        entry: dict[str, Any] = {"written": written, "relation": relation}
        if ref is not None:
            entry["target"] = ref.key
            entry["fqn"] = ref.fqn
            entry["external"] = ref.external
        arguments = resolved_arguments(index, declared.path, owner, written)
        if arguments:
            entry["arguments"] = arguments
        found.append(entry)
    return found


def _repository(bases: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The entity (and id) of a Spring Data repository, from its base's type arguments."""
    for base in bases:
        fqn = str(base.get("fqn", ""))
        name = fqn.rsplit(".", 1)[-1]
        arguments: list[dict[str, str]] = base.get("arguments", [])
        if name in REPOSITORIES and fqn.startswith("org.springframework.data") and arguments:
            repository: dict[str, Any] = {"base": fqn, "entity": arguments[0]}
            if len(arguments) > 1:
                repository["id"] = arguments[1]
            return repository
    return None


def method_attrs(index: TypeIndex, declared: Declared, method: Method) -> dict[str, Any]:
    owner = declared.type.key
    attrs: dict[str, Any] = {}
    annotations = _annotations(index, declared, owner, method.annotations)
    if annotations:
        attrs["annotations"] = annotations
    parameters = [_variable(index, declared, owner, p) for p in method.parameters]
    if parameters:
        attrs["parameters"] = parameters
    if method.returns is not None:
        attrs["returns"] = method.returns
        ref = index.resolve(declared.path, owner, method.returns)
        if ref is not None:
            attrs["returns_target"] = ref.key
    if method.static:
        attrs["static"] = True
    if method.varargs:
        attrs["varargs"] = True
    return attrs


def field_attrs(index: TypeIndex, declared: Declared, variable: Variable) -> dict[str, Any]:
    return _variable(index, declared, declared.type.key, variable)


def _variable(
    index: TypeIndex, declared: Declared, owner: str, variable: Variable
) -> dict[str, Any]:
    attrs: dict[str, Any] = {"name": variable.name, "type": variable.type or ""}
    element = bean_type(variable.type or "") or variable.type or ""
    ref = index.resolve(declared.path, owner, element)
    if ref is not None:
        attrs["target"] = ref.key
        attrs["fqn"] = ref.fqn
    arguments = resolved_arguments(index, declared.path, owner, variable.type or "")
    if arguments:
        attrs["arguments"] = arguments
    annotations = _annotations(index, declared, owner, variable.annotations)
    if annotations:
        attrs["annotations"] = annotations
    if variable.static:
        attrs["static"] = True
    return attrs


def _annotations(
    index: TypeIndex, declared: Declared, owner: str, annotations: tuple[Annotation, ...]
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for annotation in annotations:
        entry: dict[str, Any] = {"name": annotation.name, "line": annotation.line}
        ref = index.resolve(declared.path, owner, annotation.name)
        if ref is not None:
            entry["fqn"] = ref.fqn
            if not ref.external:
                entry["target"] = ref.key
        if annotation.arguments is not None:
            entry["arguments"] = annotation.arguments
        found.append(entry)
    return found


def node_attrs(
    index: TypeIndex, path: str, injects: dict[str, list[dict[str, str]]]
) -> dict[str, dict[str, Any]]:
    """The ``java`` attrs of each of a file's types, methods and fields, by key."""
    found: dict[str, dict[str, Any]] = {}
    for kind in index.facts[path].types:
        declared = index.by_key.get(kind.key)
        if declared is None or declared.path != path:
            continue
        found[kind.key] = {"java": type_attrs(index, declared, injects.get(kind.key))}
        for method in kind.methods:
            attrs = method_attrs(index, declared, method)
            if method.key in injects:
                attrs["injects"] = injects[method.key]
            found.setdefault(method.key, {"java": attrs})
        for variable in kind.fields:
            if variable.key is not None:
                found.setdefault(variable.key, {"java": field_attrs(index, declared, variable)})
    return found
