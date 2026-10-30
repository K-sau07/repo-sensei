"""Java calls (3.18): what each call of a file reaches, with how sure that is.

A call is resolved from its **receiver** (:meth:`CallResolver.value`), then the method of that
name on the receiver's type and its supertypes (:meth:`CallResolver.methods`):

- ``m()`` and ``this.m()``: the enclosing class, then its supertypes, then the classes around
  it (an inner class calls its outer one's), then what a static import names (``import static
  a.b.Util.helper``, ``import static a.b.Util.*``; of several libraries' on-demand ones, the
  one a short list knows has the name: Mockito's ``when``, JUnit's ``assertEquals``):
  ``import_resolved`` (0.9);
- a **typed receiver**: a field, a parameter or a local whose type is declared (injected fields
  among them; ``var x = new T()`` and a cast tell theirs): the method of that type or of its
  supertypes: ``import_resolved``, the type being declared. On an interface the edge goes to the
  interface's method: the trace's override step goes on to the implementations;
- ``Type.m()`` (a static call), ``new Type(...)`` (its constructor, or the class when it
  declares none), ``this(...)``/``super(...)`` and ``super.m()`` (from the superclass on):
  ``import_resolved``;
- a **chain** ``a.b().c()`` and a ``var x = a.b()``: ``c`` on what ``b`` is declared to return,
  one level deep: ``receiver_heuristic`` (0.7). Deeper, the receiver is unknown;
- a method a type of the repository **inherits from a library** (``orderRepository.save(o)``
  with ``OrderRepository extends JpaRepository<Order, Long>``): the library's method, an
  external (``external_symbol java:org.springframework.data.jpa.repository.JpaRepository.save``),
  the receiver's type on the edge (``attrs.receiver``, the in-repository type's key) for 3.20;
  a library type's method (``List.add``, ``ResponseEntity.ok``): its external, as sure as the
  receiver's type; an enum's and a record's own (``Status.valueOf``): ``java.lang.Enum``'s
  and ``java.lang.Record``'s;
- a **Lombok accessor** (``getName()`` of a ``@Data`` or ``@Getter`` class) and a **record's
  component** (``dto.name()``) aren't methods the repository has: a ``REFERENCES`` edge to the
  field, as sure as the receiver; what it gives is the field's declared type, followed on at
  ``receiver_heuristic`` at any depth (a field's type is declared, not guessed). Lombok's
  ``builder()`` of a ``@Builder`` class is a call of the class (it makes one);
- an **unknown receiver** (a lambda's parameter, an untyped chain, a name nothing binds): the
  repository's methods of that name and arity: ``name_unique`` (0.4) for one (a method and its
  overrides are one: the topmost), ``name_ambiguous`` (0.2/k) to each of k ≤ 5, else none. A
  name the JDK's common types have (``get``, ``stream``, ``toString``, ...) is never guessed,
  nor anything on a literal, an array, a library call's result or a type we can't name
  (counted apart: ``dead``).

**Overloads** are picked by argument count (varargs taking any more), then by what the
arguments surely are (a literal's type, ``new T()``, a cast, a variable's declared type) against
the parameters' types. When several still fit, there is an edge to each, its confidence split
among them (the tier's divided by their number, ``attrs.overloads`` their number).

Every edge goes from the innermost definition around the call (its method or constructor, a
field for an initializer's), carries the line of the called name (a chain's calls each their
own) and is owned by its file.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from sensei_core.models import EdgeType, Provenance
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.java.di import names
from sensei_engine.graph.java.facts import Call, JavaFacts, Method, Variable
from sensei_engine.graph.java.imports import (
    PRIMITIVES,
    Declared,
    TypeIndex,
    TypeRef,
    base_name,
    external,
    external_key,
    external_node,
    is_array,
)
from sensei_engine.graph.writer import GraphEdge, GraphNode

MAX_CANDIDATES = 5  # name_ambiguous fan-out; more and nothing is guessed
MAX_LEVEL = 1  # calls' return types followed in a chain
MAX_DEPTH = 24  # receivers resolved within one another

# Methods the JDK's common types have (Object, String, collections, streams, Optional, the
# boxes, loggers, builders): a receiver of unknown type calling one is most likely one of them.
JDK_METHODS = frozenset("""
    toString equals hashCode getClass notify notifyAll wait clone finalize compareTo
    length charAt substring trim strip isEmpty isBlank split replace replaceAll contains
    startsWith endsWith toLowerCase toUpperCase format formatted valueOf join matches indexOf
    lastIndexOf concat equalsIgnoreCase getBytes chars lines repeat
    add addAll remove removeAll removeIf retainAll get put putAll putIfAbsent getOrDefault
    containsKey containsValue keySet values entrySet size clear iterator listIterator forEach
    stream parallelStream toArray sort of copyOf set first last subList computeIfAbsent
    computeIfPresent compute merge offer poll peek push pop
    isPresent ifPresent ifPresentOrElse orElse orElseGet orElseThrow map flatMap filter collect
    reduce sorted distinct limit skip findFirst findAny anyMatch allMatch noneMatch count min max
    toList mapToInt mapToLong mapToDouble mapToObj sum average boxed getKey getValue hasNext next
    apply accept test run call close append insert build builder info debug warn error trace
    isDebugEnabled isInfoEnabled println print printf parse parseInt parseLong parseDouble now
    plus minus isBefore isAfter compare comparing thenComparing reversed asList emptyList
    emptyMap emptySet singletonList singleton unmodifiableList requireNonNull isNull nonNull hash
    ofNullable empty getMessage printStackTrace getCause name ordinal intValue longValue
    doubleValue booleanValue getTime start stop sleep interrupt
""".split())  # noqa: SIM905  # fmt: skip

_BOXES = {
    "Integer": "int", "Long": "long", "Short": "short", "Byte": "byte", "Double": "double",
    "Float": "float", "Boolean": "boolean", "Character": "char",
}  # fmt: skip
_NUMERIC = ("byte", "short", "char", "int", "long", "float", "double")
_GETTERS = frozenset({"Data", "Getter", "Value"})
_SETTERS = frozenset({"Data", "Setter"})
_BUILDERS = frozenset({"Builder", "SuperBuilder"})
# What the test libraries usually imported statically on demand give (several at once:
# import static org.mockito.Mockito.*; import static org.junit.jupiter.api.Assertions.*;).
WELL_KNOWN_STATIC: Mapping[str, frozenset[str]] = {
    "org.mockito.Mockito": frozenset({
        "mock", "spy", "when", "verify", "times", "never", "atLeast", "atLeastOnce", "atMost",
        "doReturn", "doThrow", "doNothing", "doAnswer", "reset", "verifyNoInteractions",
        "verifyNoMoreInteractions", "inOrder", "lenient", "mockStatic",
    }),
    "org.mockito.ArgumentMatchers": frozenset({
        "any", "anyString", "anyLong", "anyInt", "anyBoolean", "anyList", "anyMap", "eq",
        "argThat", "isNull", "notNull", "isA", "contains",
    }),
    "org.junit.jupiter.api.Assertions": frozenset({
        "assertEquals", "assertNotEquals", "assertTrue", "assertFalse", "assertNull",
        "assertNotNull", "assertThrows", "assertDoesNotThrow", "assertSame", "assertNotSame",
        "assertAll", "assertArrayEquals", "assertIterableEquals", "assertTimeout", "fail",
    }),
    "org.junit.Assert": frozenset({
        "assertEquals", "assertNotEquals", "assertTrue", "assertFalse", "assertNull",
        "assertNotNull", "assertSame", "assertNotSame", "assertArrayEquals", "assertThat",
        "fail",
    }),
    "org.assertj.core.api.Assertions": frozenset({
        "assertThat", "assertThatThrownBy", "assertThatCode", "catchThrowable", "fail", "tuple",
        "entry",
    }),
    "org.hamcrest.Matchers": frozenset({
        "is", "equalTo", "hasSize", "hasItem", "hasItems", "containsString", "notNullValue",
        "nullValue", "not", "greaterThan", "lessThan", "empty",
    }),
    "org.springframework.test.web.servlet.request.MockMvcRequestBuilders": frozenset({
        "get", "post", "put", "patch", "delete", "request", "multipart",
    }),
    "org.springframework.test.web.servlet.result.MockMvcResultMatchers": frozenset({
        "status", "jsonPath", "content", "header", "redirectedUrl", "view", "model",
    }),
}  # fmt: skip

ValueKind = Literal["instance", "type", "unknown", "dead"]


@dataclass(frozen=True, slots=True)
class Value:
    """What an expression is: an instance of a type, a type itself (a static call's receiver),
    of a type we don't know (a method may be guessed by name) or a dead end (a literal, a
    primitive, a library call's result: nothing the graph follows)."""

    kind: ValueKind
    ref: TypeRef | None = None
    level: int = 0  # calls' return types it came through (receiver_heuristic when > 0)
    super_of: bool = False  # super: its methods from the superclass on


UNKNOWN = Value("unknown")
DEAD = Value("dead")


@dataclass(frozen=True, slots=True)
class Hit:
    key: str
    provenance: Provenance
    confidence: float
    type: EdgeType = EdgeType.CALLS
    attrs: Mapping[str, Any] = field(default_factory=dict[str, Any])


# dead: on what the graph doesn't follow (a library call's result, a literal, a primitive)
Status = Literal["resolved", "guessed", "external", "unresolved", "dead"]


@dataclass(frozen=True, slots=True)
class Resolution:
    hits: tuple[Hit, ...]
    result: Value  # what the call gives (the receiver of a call chained on it)
    status: Status


@dataclass(slots=True)
class CallStats:
    calls: int = 0  # call sites
    resolved: int = 0  # ... to the repository's methods, constructors, classes, fields
    external: int = 0  # ... to a library's (an inherited method, a JDK type's)
    guessed: int = 0  # ... by name only (name_unique, name_ambiguous)
    unresolved: int = 0  # ... to nothing the graph has
    dead: int = 0  # ... on what it doesn't follow (a library call's result, a literal)
    overloads: int = 0  # sites several overloads still fit (an edge to each)
    accessors: int = 0  # Lombok accessors and record components (REFERENCES to the field)
    by_provenance: Counter[str] = field(default_factory=Counter[str])  # edges


@dataclass(frozen=True, slots=True)
class _Member:
    declared: Declared
    method: Method


class CallResolver:
    """Resolves a project's Java calls (see the module)."""

    def __init__(self, index: TypeIndex) -> None:
        self.index = index
        self._locals: dict[str, list[Variable]] = {}
        self._callables: dict[str, _Member] = {}
        self._by_name: dict[str, list[_Member]] = {}
        for path in sorted(index.facts):
            file = index.facts[path]
            for local in file.locals:
                self._locals.setdefault(local.owner or "", []).append(local)
            for declared in file.types:
                entry = index.by_key.get(declared.key)
                if entry is None or entry.path != path:
                    continue
                for method in declared.methods:
                    member = _Member(entry, method)
                    self._callables.setdefault(method.key, member)
                    if not method.constructor:
                        self._by_name.setdefault(method.name, []).append(member)
        self._memo: dict[tuple[str, int], Resolution] = {}
        self._resolving: set[tuple[str, int]] = set()
        self.stats = CallStats()

    # ------------------------------------------------------------------ calls

    def resolve(self, path: str, index: int, depth: int = 0) -> Resolution:
        """What call ``index`` of file ``path`` reaches (memoized: chains ask again)."""
        memo_key = (path, index)
        held = self._memo.get(memo_key)
        if held is not None:
            return held
        if memo_key in self._resolving or depth > MAX_DEPTH:
            return Resolution((), UNKNOWN, "unresolved")
        self._resolving.add(memo_key)
        call = self.index.facts[path].calls[index]
        if call.kind in ("new", "reference_new"):
            found = self._new(path, call)
        elif call.kind in ("this", "super"):
            found = self._constructor_call(call)
        else:
            receiver = self.value(path, call, depth)
            found = self._on(path, call, receiver)
        self._resolving.discard(memo_key)
        self._memo[memo_key] = found
        return found

    def _new(self, path: str, call: Call) -> Resolution:
        ref = self.index.resolve(path, call.owner, call.name)
        if ref is None:
            return Resolution((), DEAD, "unresolved")
        instance = Value("instance", ref)
        if ref.external:
            return Resolution((_hit(ref.key, Provenance.IMPORT_RESOLVED),), instance, "external")
        declared = self.index.by_key[ref.key]
        constructors = [m for m in declared.type.methods if m.constructor]
        if not constructors:
            return Resolution((_hit(ref.key, Provenance.IMPORT_RESOLVED),), instance, "resolved")
        chosen = self._overload(path, call, [_Member(declared, m) for m in constructors])
        hits = _split(chosen, Provenance.IMPORT_RESOLVED) or (
            _hit(ref.key, Provenance.IMPORT_RESOLVED),
        )
        return Resolution(hits, instance, "resolved")

    def _constructor_call(self, call: Call) -> Resolution:
        """``this(...)``/``super(...)``: a constructor of the class or its superclass."""
        own = self.index.by_key.get(call.owner or "")
        if own is None:
            return Resolution((), DEAD, "unresolved")
        target = own if call.kind == "this" else None
        if call.kind == "super":
            parent = self.index.superclass(own.type.key)
            if parent is None or parent.external:  # Object's, a library class's
                return Resolution((), DEAD, "dead")
            target = self.index.by_key.get(parent.key)
        if target is None:
            return Resolution((), DEAD, "unresolved")
        constructors = [
            _Member(target, m)
            for m in target.type.methods
            if m.constructor and m.key != call.callable
        ]
        chosen = self._overload(own.path, call, constructors)
        if not chosen:
            return Resolution((), DEAD, "unresolved")
        return Resolution(_split(chosen, Provenance.IMPORT_RESOLVED), DEAD, "resolved")

    def _on(self, path: str, call: Call, receiver: Value | None) -> Resolution:
        """A method called on a receiver (None: no receiver, ``m()``)."""
        if receiver is None:
            return self._implicit(path, call)
        if receiver.kind == "unknown":
            return self._guess(path, call)
        if receiver.kind == "dead" or receiver.ref is None:
            return Resolution((), DEAD, "dead")
        how = Provenance.RECEIVER_HEURISTIC if receiver.level else Provenance.IMPORT_RESOLVED
        ref = receiver.ref
        if ref.external:
            return Resolution(
                (_hit(external_key(f"{ref.fqn}.{call.name}"), how),), DEAD, "external"
            )
        return self._members(path, call, ref, how, receiver.level, skip_own=receiver.super_of)

    def _members(
        self,
        path: str,
        call: Call,
        ref: TypeRef,
        how: Provenance,
        level: int,
        *,
        skip_own: bool = False,
    ) -> Resolution:
        """A method of one of the repository's types (or its supertypes'), else an accessor,
        else the library method it inherits."""
        found, library = self.methods(path, call, ref, skip_own=skip_own)
        if found:
            return Resolution(_split(found, how), self._returned(found, level), "resolved")
        built = self._builder(ref, call)
        if built is not None:  # Lombok's Order.builder(): makes an Order
            return Resolution(
                (Hit(built, how, confidence_of(how), attrs={"lombok": "builder"}),),
                DEAD,
                "resolved",
            )
        accessor = self._accessor(ref, call)
        if accessor is not None:
            variable, declared = accessor
            hit = Hit(variable.key or "", how, confidence_of(how), EdgeType.REFERENCES)
            returned = self._typed(declared.path, declared.type.key, variable.type, level + 1)
            return Resolution((hit,), returned, "resolved")
        if library is not None:
            hit = Hit(
                external_key(f"{library.fqn}.{call.name}"),
                how,
                confidence_of(how),
                attrs={"receiver": ref.key},
            )
            return Resolution((hit,), DEAD, "external")
        return Resolution((), DEAD, "unresolved")

    def _implicit(self, path: str, call: Call) -> Resolution:
        """``m()``: the class and its supertypes, the classes around it, static imports, then
        what the class inherits from a library."""
        owner = self.index.by_key.get(call.owner or "")
        library: TypeRef | None = None
        while owner is not None:
            found, inherited = self.methods(path, call, owner.ref())
            if found:
                result = self._returned(found, 0)
                return Resolution(_split(found, Provenance.IMPORT_RESOLVED), result, "resolved")
            library = library or inherited
            owner = self.index.by_key.get(owner.type.outer or "")
        imported = self._static_import(path, call)
        if imported is not None:
            return imported
        if library is not None:
            hit = Hit(
                external_key(f"{library.fqn}.{call.name}"),
                Provenance.IMPORT_RESOLVED,
                confidence_of(Provenance.IMPORT_RESOLVED),
                attrs={"receiver": call.owner},
            )
            return Resolution((hit,), DEAD, "external")
        return Resolution((), DEAD, "unresolved")

    def _static_import(self, path: str, call: Call) -> Resolution | None:
        file = self.index.facts[path]
        libraries: list[str] = []
        for wildcard in (False, True):
            for item in file.imports:
                if item.kind != ("static_wildcard" if wildcard else "static"):
                    continue
                if not wildcard and item.name != call.name:
                    continue
                declared = self.index.declared(item.source, near=path)
                if declared is None:
                    if not wildcard:
                        return _external(f"{item.source}.{call.name}")
                    libraries.append(item.source)
                    continue
                found, _ = self.methods(path, call, declared.ref())
                if found:
                    return Resolution(
                        _split(found, Provenance.IMPORT_RESOLVED),
                        self._returned(found, 0),
                        "resolved",
                    )
        if len(libraries) > 1:  # several: the one known to have it
            libraries = [s for s in libraries if call.name in WELL_KNOWN_STATIC.get(s, ())]
        if len(libraries) == 1:  # import static org.junit.Assert.*: assertEquals
            return _external(f"{libraries[0]}.{call.name}")
        return None

    def _guess(self, path: str, call: Call) -> Resolution:
        """An unknown receiver's method, by its name (see the module)."""
        if call.name in JDK_METHODS:
            return Resolution((), UNKNOWN, "unresolved")
        candidates = [m for m in self._by_name.get(call.name, ()) if _fits(m.method, call)]
        if len(candidates) > 4 * MAX_CANDIDATES:  # too common a name, even with overrides one
            return Resolution((), UNKNOWN, "unresolved")
        candidates = self._topmost(candidates)
        if not 1 <= len(candidates) <= MAX_CANDIDATES:
            return Resolution((), UNKNOWN, "unresolved")
        how = Provenance.NAME_UNIQUE if len(candidates) == 1 else Provenance.NAME_AMBIGUOUS
        k = len(candidates)
        hits = tuple(Hit(m.method.key, how, confidence_of(how, k)) for m in candidates)
        return Resolution(hits, UNKNOWN, "guessed")

    def _topmost(self, candidates: list[_Member]) -> list[_Member]:
        """The candidates no other one overrides from a supertype (an implementation's method
        and its interface's are one: the interface's)."""
        owners = {m.declared.type.key for m in candidates}
        kept: list[_Member] = []
        for member in candidates:
            above = self.index.hierarchy(member.declared.ref(), skip_self=True)
            if not any(r.key in owners for r in above):
                kept.append(member)
        return kept

    # ------------------------------------------------------------------ methods

    def methods(
        self, path: str, call: Call, ref: TypeRef, *, skip_own: bool = False
    ) -> tuple[list[_Member], TypeRef | None]:
        """The methods a call on ``ref`` may mean: those of the nearest type of its hierarchy
        with ones of the name and arity (then picked by argument, :meth:`_overload`), and the
        first library type it inherits (where a method it doesn't declare comes from)."""
        library: TypeRef | None = None
        for current in self.index.hierarchy(ref, skip_self=skip_own):
            if current.external:
                library = library or current
                continue
            declared = self.index.by_key.get(current.key)
            if declared is None:
                continue
            named = [
                _Member(declared, m)
                for m in declared.type.methods
                if m.name == call.name and not m.constructor and _fits(m, call)
            ]
            if named:
                return self._overload(path, call, named), library
        if library is None:  # an enum's values() and name(), a record's: java.lang's
            declared = None if ref.external else self.index.by_key.get(ref.key)
            kind = declared.type.kind if declared is not None else None
            if kind in ("enum", "record"):
                library = external(f"java.lang.{kind.capitalize()}")
        return [], library

    def _overload(self, path: str, call: Call, members: list[_Member]) -> list[_Member]:
        """The overloads a call's arguments fit (see the module)."""
        fitting = [m for m in members if _fits(m.method, call)]
        if len(fitting) <= 1 or call.arguments is None:
            return fitting
        scored: list[tuple[int, _Member]] = []
        for member in fitting:
            score = self._score(path, call, member)
            if score is not None:
                scored.append((score, member))
        if not scored:
            return fitting
        best = max(s for s, _ in scored)
        return [m for s, m in scored if s == best]

    def _score(self, path: str, call: Call, member: _Member) -> int | None:
        """How well the arguments fit a method's parameters (None: one surely doesn't)."""
        parameters = member.method.parameters
        score = 0
        for position, argument in enumerate(call.arguments or ()):
            if not parameters:
                return None
            parameter = parameters[min(position, len(parameters) - 1)]
            wanted = parameter.type or ""
            if member.method.varargs and position >= len(parameters) - 1:
                wanted = wanted.removesuffix("[]")
            told = self._argument_type(path, call, argument)
            if told is None:
                continue
            fit = self._fit(path, call, told, member, wanted)
            if fit is None:
                return None
            score += fit
        return score

    def _argument_type(self, path: str, call: Call, argument: str) -> str | None:
        if argument.startswith("="):
            return argument[1:]
        if argument.startswith("$"):
            variable = self._variable(path, call, argument[1:])
            return variable.type if variable is not None else None
        return None

    def _fit(self, path: str, call: Call, told: str, member: _Member, wanted: str) -> int | None:
        """2: the same type; 1: a wider one (Object, a supertype, a wider number); 0: can't
        tell; None: surely not."""
        simple = _simple_fit(told, wanted)
        if simple != _BY_TYPES:
            return simple
        want = base_name(wanted)
        if want in member.method.type_parameters or self.index.is_type_variable(
            member.declared.path, member.declared.type.key, want, member.method.key
        ):
            return 1
        mine = self.index.resolve(path, call.owner, told)
        theirs = self.index.resolve(member.declared.path, member.declared.type.key, wanted)
        if mine is None or theirs is None or mine.external or theirs.external:
            return 0
        above = {r.key for r in self.index.hierarchy(mine)}
        return 1 if theirs.key in above else None

    def _returned(self, found: list[_Member], level: int) -> Value:
        """What calling the methods gives: their declared return type (one level deep)."""
        returns = {m.method.returns for m in found}
        if len(returns) != 1 or level >= MAX_LEVEL:
            return UNKNOWN
        member = found[0]
        if member.method.returns and member.method.returns in member.method.type_parameters:
            return UNKNOWN
        return self._typed(
            member.declared.path, member.declared.type.key, member.method.returns, level + 1
        )

    def _builder(self, ref: TypeRef, call: Call) -> str | None:
        """The class Lombok's ``builder()``/``toBuilder()`` of a ``@Builder`` class makes."""
        if call.name not in ("builder", "toBuilder") or call.arguments:
            return None
        for current in self.index.hierarchy(ref):
            declared = None if current.external else self.index.by_key.get(current.key)
            if declared is not None and names(declared.type.annotations) & _BUILDERS:
                return ref.key
        return None

    def _accessor(self, ref: TypeRef, call: Call) -> tuple[Variable, Declared] | None:
        """The field a Lombok accessor or a record's component accessor reads or writes."""
        for current in self.index.hierarchy(ref):
            declared = None if current.external else self.index.by_key.get(current.key)
            if declared is None:
                continue
            for variable in declared.type.fields:
                if variable.static or variable.key is None:
                    continue
                if _accesses(declared, variable, call):
                    self.stats.accessors += 1
                    return variable, declared
        return None

    # ------------------------------------------------------------------ receivers

    def value(self, path: str, call: Call, depth: int = 0) -> Value | None:
        """What a call's receiver is (None: it has none)."""
        receiver = call.receiver
        kind = receiver.kind
        if kind in ("none", "this", "super"):
            return None if kind == "none" else self._instance(call.owner, super_of=kind == "super")
        if kind == "call":  # what the call it is made on returns
            return self.resolve(path, receiver.call, depth + 1).result if receiver.call >= 0 else (
                UNKNOWN
            )  # fmt: skip
        if kind in ("new", "cast"):
            ref = self.index.resolve(path, call.owner, receiver.parts[0])
            return Value("instance", ref) if ref is not None else DEAD
        if kind == "name":
            return self._chain(path, call, receiver.parts, depth)
        return DEAD if kind == "dead" else UNKNOWN

    def _instance(self, key: str | None, *, super_of: bool = False) -> Value:
        declared = self.index.by_key.get(key or "")
        if declared is None:
            return DEAD
        return Value("instance", declared.ref(), super_of=super_of)

    def _chain(self, path: str, call: Call, parts: tuple[str, ...], depth: int) -> Value:
        """A name chain's value: its first name (a variable, a field, a type, a package), then
        each field or member type after it."""
        first, rest = self._first(path, call, parts, depth)
        for name in rest:
            first = self._attribute(first, name)
        return first

    def _first(
        self, path: str, call: Call, parts: tuple[str, ...], depth: int
    ) -> tuple[Value, tuple[str, ...]]:
        """A chain's first name's value (a variable, a field, a type), and the names after it
        still to follow."""
        name = parts[0]
        if name == "this":
            return self._instance(call.owner), parts[1:]
        if len(parts) >= 2 and parts[-1] == "this":  # Outer.this
            ref = self.index.resolve(path, call.owner, ".".join(parts[:-1]))
            return (Value("instance", ref) if ref else DEAD), ()
        variable = self._variable(path, call, name)
        if variable is not None:
            return self._variable_value(path, call, variable, depth), parts[1:]
        imported = self._static_field(path, name)
        if imported is not None:
            return imported, parts[1:]
        ref = self.index.resolve(path, call.owner, name)
        if ref is not None:
            return Value("type", ref), parts[1:]
        return self._qualified(path, parts)

    def _qualified(self, path: str, parts: tuple[str, ...]) -> tuple[Value, tuple[str, ...]]:
        """A chain starting with a package: ``com.acme.Util.helper()`` (written in ``path``)."""
        for end in range(len(parts), 1, -1):
            declared = self.index.declared(".".join(parts[:end]), near=path)
            if declared is not None:
                return Value("type", declared.ref()), parts[end:]
        name = parts[0]
        if name[:1].islower() and len(parts) > 1 and parts[-1][:1].isupper():
            return Value("type", self.index.fqn(".".join(parts), near=path)), ()
        if name[:1].isupper() and not name.isupper():
            return DEAD, ()  # a type we can't name (a library's, imported on demand)
        return UNKNOWN, ()

    def _variable(self, path: str, call: Call, name: str) -> Variable | None:
        """The local, parameter or field a name means where the call is."""
        if name.startswith("this."):
            return self._field(call.owner, name.removeprefix("this."))
        best: Variable | None = None
        for local in self._locals.get(call.callable or "", ()):
            if (
                local.name == name
                and local.line <= call.line <= local.end_line
                and (best is None or local.line >= best.line)
            ):
                best = local
        if best is not None:
            return best
        member = self._callables.get(call.callable or "")
        if member is not None:
            parameter = next((p for p in member.method.parameters if p.name == name), None)
            if parameter is not None:
                return parameter
        owner = call.owner
        while owner:
            found = self._field(owner, name)
            if found is not None:
                return found
            declared = self.index.by_key.get(owner)
            owner = declared.type.outer if declared is not None else None
        return None

    def _field(self, owner: str | None, name: str) -> Variable | None:
        declared = self.index.by_key.get(owner or "")
        if declared is None:
            return None
        for current in self.index.hierarchy(declared.ref()):
            held = None if current.external else self.index.by_key.get(current.key)
            for variable in held.type.fields if held is not None else ():
                if variable.name == name:
                    return variable
        return None

    def _variable_value(self, path: str, call: Call, variable: Variable, depth: int) -> Value:
        if variable.type is None:
            if variable.value >= 0:  # var x = a.b(): what b returns
                return self.resolve(path, variable.value, depth + 1).result
            return UNKNOWN
        where, owner = (path, call.owner)
        if variable.key is not None:
            where, owner = self._field_scope(variable, path, call.owner)
        if self.index.is_type_variable(where, owner, base_name(variable.type), call.callable):
            return UNKNOWN
        return self._typed(where, owner, variable.type, 0)

    def _field_scope(
        self, variable: Variable, path: str, owner: str | None
    ) -> tuple[str, str | None]:
        """The file and type a field is declared in (its type is named there)."""
        key = variable.key or ""
        container = key[: key.rfind("#") + 1] if "#" in key else ""
        declared = self.index.by_key.get(container)
        if declared is not None:
            return declared.path, declared.type.key
        return path, owner

    def _typed(self, path: str, owner: str | None, written: str | None, level: int) -> Value:
        """An instance of a declared type (as written in ``path``, inside ``owner``)."""
        if not written or base_name(written) in PRIMITIVES or is_array(written):
            return DEAD
        if self.index.is_type_variable(path, owner, base_name(written), None):
            return UNKNOWN
        ref = self.index.resolve(path, owner, written)
        return Value("instance", ref, level) if ref is not None else DEAD

    def _static_field(self, path: str, name: str) -> Value | None:
        """A field a static import names (``import static a.b.Config.DEFAULTS``)."""
        for item in self.index.facts[path].imports:
            if item.kind != "static" or item.name != name:
                continue
            declared = self.index.declared(item.source, near=path)
            if declared is None:
                return DEAD
            variable = next((f for f in declared.type.fields if f.name == name), None)
            if variable is not None:
                return self._typed(declared.path, declared.type.key, variable.type, 0)
        return None

    def _attribute(self, value: Value, name: str) -> Value:
        """``<value>.<name>``: a member type or a static field of a type, an instance's
        field."""
        if value.kind in ("unknown", "dead") or value.ref is None:
            return value
        ref = value.ref
        if ref.external:
            if value.kind == "type" and name[:1].isupper() and not name.isupper():
                return Value("type", self.index.member_type(ref, name))
            return DEAD  # a library's field: of a type we don't know
        if value.kind == "type":
            member = self.index.member_type(ref, name)
            if member is not None:
                return Value("type", member)
        variable = self._field(ref.key, name)
        if variable is None:
            return DEAD
        path, owner = self._field_scope(variable, "", ref.key)
        return self._typed(path, owner, variable.type, value.level)

    # ------------------------------------------------------------------ edges

    def edges(self, path: str, nodes: dict[str, GraphNode]) -> list[GraphEdge]:
        """A file's call edges (one per ends, type and line: the surest stays)."""
        file = self.index.facts[path]
        found: dict[tuple[str, str, EdgeType, int], GraphEdge] = {}
        for position, call in enumerate(file.calls):
            resolution = self.resolve(path, position)
            self._count(resolution)
            if len(resolution.hits) > 1 and resolution.status == "resolved":
                self.stats.overloads += 1
            for hit in resolution.hits:
                if not hit.key:
                    continue
                if hit.key.startswith("external_symbol "):
                    nodes.setdefault(hit.key, external_node(hit.key))
                attrs = dict(hit.attrs)
                if call.kind.startswith("reference"):
                    attrs["reference"] = True
                edge = GraphEdge(
                    call.source,
                    hit.key,
                    hit.type,
                    hit.provenance,
                    confidence=hit.confidence,
                    evidence_path=path,
                    evidence_line=call.line,
                    attrs=attrs,
                    owner=path,
                )
                slot = (call.source, hit.key, hit.type, call.line)
                held = found.get(slot)
                if held is None or (held.confidence or 0) < hit.confidence:
                    found[slot] = edge
        for edge in found.values():
            self.stats.by_provenance[edge.provenance.value] += 1
        return list(found.values())

    def _count(self, resolution: Resolution) -> None:
        self.stats.calls += 1
        if resolution.status == "resolved":
            self.stats.resolved += 1
        elif resolution.status == "external":
            self.stats.external += 1
        elif resolution.status == "guessed":
            self.stats.guessed += 1
        elif resolution.status == "dead":
            self.stats.dead += 1
        else:
            self.stats.unresolved += 1


def _hit(key: str, how: Provenance, **attrs: Any) -> Hit:
    return Hit(key, how, confidence_of(how), attrs=attrs)


def _external(fqn: str) -> Resolution:
    return Resolution((_hit(external_key(fqn), Provenance.IMPORT_RESOLVED),), DEAD, "external")


def _split(found: list[_Member], how: Provenance) -> tuple[Hit, ...]:
    """An edge to each method; several (overloads the call still fits): the confidence split."""
    k = len(found)
    if k == 1:
        return (Hit(found[0].method.key, how, confidence_of(how)),)
    return tuple(
        Hit(m.method.key, how, confidence_of(how) / k, attrs={"overloads": k}) for m in found
    )


def _fits(method: Method, call: Call) -> bool:
    """Whether a method takes as many arguments as the call has (a reference: any)."""
    if call.arguments is None:
        return True
    count, wanted = len(call.arguments), len(method.parameters)
    if method.varargs:
        return count >= wanted - 1
    return count == wanted


def _unboxed(name: str) -> str:
    return _BOXES.get(name, name)


_BY_TYPES = -1  # _simple_fit: the types decide
_PLAIN = frozenset({"String", "boolean", *_NUMERIC})


def _simple_fit(told: str, wanted: str) -> int | None:
    """:meth:`CallResolver._fit` by the names alone (literals, numbers, ``null``, ``Object``);
    ``_BY_TYPES`` when it takes the types."""
    have, want = _unboxed(base_name(told)), _unboxed(base_name(wanted))
    if not want or not have:
        return 0
    if is_array(told) != is_array(wanted) or (have == "null" and want in PRIMITIVES):
        return None
    if have == want:
        return 2
    if have == "null" or want == "Object":
        return 1
    if have in _NUMERIC and want in _NUMERIC:  # a widening
        return 1 if _NUMERIC.index(have) < _NUMERIC.index(want) else None
    return None if have in _PLAIN and want in _PLAIN else _BY_TYPES


def _accesses(declared: Declared, variable: Variable, call: Call) -> bool:
    """Whether a call is the accessor of a field: a record component's ``x()``, Lombok's
    ``getX()``/``isX()`` (``@Getter``, ``@Data``, ``@Value``) or ``setX(v)``."""
    arguments = len(call.arguments or ())
    if declared.type.kind == "record":
        return call.name == variable.name and arguments == 0
    marks = names(declared.type.annotations) | names(variable.annotations)
    capital = variable.name[:1].upper() + variable.name[1:]
    if marks & _GETTERS and arguments == 0:
        prefixes = ("get", "is") if base_name(variable.type or "") == "boolean" else ("get",)
        if call.name in {f"{p}{capital}" for p in prefixes}:
            return True
    if marks & _SETTERS and arguments == 1 and not variable.final:
        return call.name == f"set{capital}"
    return False


def resolve_calls(
    index: TypeIndex, facts: Mapping[str, JavaFacts], nodes: dict[str, GraphNode]
) -> tuple[list[GraphEdge], CallStats, CallResolver]:
    """The project's Java call edges (``CALLS``, and ``REFERENCES`` for accessors), adding the
    externals they reach to ``nodes``."""
    resolver = CallResolver(index)
    edges: list[GraphEdge] = []
    for path in sorted(facts):
        edges.extend(resolver.edges(path, nodes))
    return edges, resolver.stats, resolver
