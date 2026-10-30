"""Spring MVC routes to ``ApiEndpoint`` nodes and ``HANDLED_BY`` edges to their handlers (3.19),
for Java (WebFlux's annotated controllers are read the same way).

**Controllers**: a class with ``@RestController``, or ``@Controller`` whose responses are bodies
(``@ResponseBody`` on the class or the method, or a ``ResponseEntity``/``HttpEntity`` returned);
a ``@Controller`` method that returns a view's name serves a page, not an API, and is left out
(counted). An annotation is Spring's by the FQN the file's imports give it (3.18's
:class:`~sensei_engine.graph.java.imports.TypeIndex`), or by its name when no import names it;
one of the repository's own, or another library's of the same name, is not.

**Mappings**: ``@GetMapping``, ``@PostMapping``, ``@PutMapping``, ``@DeleteMapping``,
``@PatchMapping`` and ``@RequestMapping(method = ...)`` (one method or several) on a method,
under the class's ``@RequestMapping``. Without ``method``, a ``@RequestMapping`` takes every
method: an endpoint for each of :data:`ANY_METHODS` (``attrs.any_method``), and a mapping that
names the method wins its path. The paths are the ``value`` or ``path`` argument: a string, an
array (an endpoint per class path and method path), a ``static final`` constant of the class, a
type around it or above it, a static import or another class (``Paths.API``), or a
concatenation of those; a path that isn't one of them is left out (``unresolved``, with where
it is). A mapping declared on an interface the controller implements (or a class it extends) is
the controller's method's, as Spring reads it, and so is the class's prefix.

**Paths**: ``{id}`` and ``{id:[0-9]+}`` become ``{}``, ``{*rest}`` and ``**`` the catch-all
``{*}``, ``*`` a ``{}``; the endpoint's key is ``api_endpoint <METHOD> <path_norm>``, as
FastAPI's (:func:`sensei_engine.link.fastapi.normalize_path`'s form).

**The servlet's prefix**: ``server.servlet.context-path``, then ``spring.mvc.servlet.path``
(``spring.webflux.base-path`` for WebFlux; Spring Boot 1's ``server.context-path`` and
``server.servlet-path``), from the ``application.properties``/``application.yml`` of the
controller's module (the nearest above it; a test's resources aren't read). The default
profile's is the URL's: it is prepended (``attrs.context_path``). One only a profile sets
(``application-prod.yml``, a document with ``spring.config.activate.on-profile``) holds only when
that profile is active: it is the endpoint's ``root_path``, and 3.10 matches calls with it and
without it. ``${NAME:/api}`` is its default.

``HANDLED_BY`` from each endpoint to its method is ``contract`` (1.0): the annotation on the
method is the route, as Spring builds its handler mappings from it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from posixpath import basename, dirname
from typing import Any, cast

import yaml
from tree_sitter import Node

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph import java
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.java.facts import Annotation, JavaFacts, Method
from sensei_engine.graph.java.imports import Declared, TypeIndex, TypeRef
from sensei_engine.graph.project import Project
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.link.pytree import line, walk
from sensei_engine.parse.parser import ParseResult

WEB = "org.springframework.web.bind.annotation"
STEREOTYPE = "org.springframework.stereotype"
VERBS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"})
ANY_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")  # a @RequestMapping with no method
BODIES = ("ResponseEntity", "HttpEntity")  # what a @Controller method returns as a body
# The settings that put a prefix before every route, in the order they apply (relaxed: no case,
# no dashes or underscores).
PREFIX_SETTINGS = (
    ("servercontextpath", "serverservletcontextpath"),
    ("serverservletpath", "springmvcservletpath", "springwebfluxbasepath"),
)
MAX_DEPTH = 8  # constants defined by constants
MAX_PATHS = 32  # a mapping's paths (arrays of arrays)
MAX_CONSTANT = 1000  # characters of a constant's initializer kept
MAX_SETTINGS = 10_000  # settings read from one YAML document
MAX_PATH_CHARS = 2048  # a route path built from constants longer than this is unresolved

_CONFIG_RE = re.compile(r"^application(?:-([\w.-]+))?\.(?:properties|ya?ml)$")
_PLACEHOLDER_RE = re.compile(r"\$\{[^}:]*(?::([^}]*))?\}")
_TOKEN_RE = re.compile(
    r"""\s*(?:("(?:[^"\\]|\\.)*")|('(?:[^'\\]|\\.)*')|([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)|(\S))""",
    re.DOTALL,
)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "s": " ", "0": "\0"}


# ---------------------------------------------------------------- what one file says


@dataclass(frozen=True, slots=True)
class FileSpring:
    """One Java file's facts for the routes (picklable: they leave the pool): 3.18's, without
    its calls and locals, and the initializers of its constant fields."""

    path: str
    facts: JavaFacts
    constants: dict[str, str] = field(default_factory=dict[str, str])  # field key: its source


def _initializers(result: ParseResult) -> dict[tuple[int, str], str]:
    """Each field declarator's initializer, by the declaration's line and the field's name."""
    found: dict[tuple[int, str], str] = {}
    tree = result.tree
    if tree is None:
        return found
    for node in walk(tree.root_node):
        if node.type not in ("field_declaration", "constant_declaration"):
            continue
        for declarator in node.children_by_field_name("declarator"):
            name, value = (
                declarator.child_by_field_name("name"),
                declarator.child_by_field_name("value"),
            )
            if name is not None and value is not None:
                found[(line(node), _source(result, name))] = _source(result, value)
    return found


def _source(result: ParseResult, node: Node) -> str:
    return result.text(node)[:MAX_CONSTANT]


def scan(result: ParseResult) -> FileSpring | None:
    """A parsed Java file's facts for the routes (None: another language, or no parse)."""
    facts = java.extract(result)
    if facts is None:
        return None
    initializers = _initializers(result)
    constants = {
        variable.key: initializers[(variable.line, variable.name)]
        for kind in facts.types
        for variable in kind.fields
        if variable.key is not None
        and variable.static
        and variable.final
        and (variable.line, variable.name) in initializers
    }
    slim = replace(facts, calls=(), locals=(), type_names=frozenset[str]())
    return FileSpring(result.path, slim, constants)


# ---------------------------------------------------------------- annotation arguments


@dataclass(frozen=True, slots=True)
class Lit:
    text: str


@dataclass(frozen=True, slots=True)
class Name:
    dotted: str  # "BASE", "Paths.API", "RequestMethod.GET"


@dataclass(frozen=True, slots=True)
class Concat:
    parts: tuple[Value, ...]


@dataclass(frozen=True, slots=True)
class Array:
    items: tuple[Value, ...]


Value = Lit | Name | Concat | Array


class _Unknown(Exception):
    """An expression the reader doesn't evaluate (a call, a ternary, a number)."""


def _unescape(body: str) -> str:
    def one(found: re.Match[str]) -> str:
        escaped = found[1]
        if escaped.startswith("u"):
            return chr(int(escaped.lstrip("u")[:4], 16))
        return _ESCAPES.get(escaped, escaped)

    return re.sub(r"\\(u+[0-9a-fA-F]{4}|.)", one, body)


def _tokens(text: str) -> list[str]:
    found: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        if match[1] is not None:
            found.append(match[1])
        elif match[2] is not None:
            raise _Unknown(text)  # a char literal: no path
        elif match[3] is not None:
            found.append(re.sub(r"\s+", "", match[3]))
        elif match[4] is not None:
            found.append(match[4])
    return found


class _Reader:
    """``value := term ('+' term)*``; ``term := string | name | '{' values '}' | '(' value ')'``."""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.at = 0

    def peek(self) -> str | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> str:
        token = self.peek()
        if token is None:
            raise _Unknown("end")
        self.at += 1
        return token

    def value(self) -> Value:
        parts = [self.term()]
        while self.peek() == "+":
            self.take()
            parts.append(self.term())
        return parts[0] if len(parts) == 1 else Concat(tuple(parts))

    def term(self) -> Value:
        piece = self.take()
        if piece.startswith('"'):
            return Lit(_unescape(piece[1:-1]))
        if piece == "{":
            items: list[Value] = []
            while self.peek() not in ("}", None):
                items.append(self.value())
                if self.peek() == ",":
                    self.take()
            self.take()
            return Array(tuple(items))
        if piece == "(":
            inner = self.value()
            if self.take() != ")":
                raise _Unknown("(")
            return inner
        if piece[:1].isalpha() or piece[:1] in "_$":
            if self.peek() == "(":
                raise _Unknown(piece)  # a call
            return Name(piece)
        raise _Unknown(piece)


def parse_value(text: str) -> Value | None:
    """A Java expression of strings, names, arrays and ``+`` (None: anything else)."""
    try:
        reader = _Reader(_tokens(text))
        found = reader.value()
    except _Unknown:
        return None
    return found if reader.peek() is None else None


def _split(text: str) -> list[str]:
    """An argument list's elements, at its top-level commas."""
    pieces: list[str] = []
    depth = 0
    quote = ""
    start = 0
    escaped = False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        elif ch == "," and depth == 0:
            pieces.append(text[start:i])
            start = i + 1
    pieces.append(text[start:])
    return [p.strip() for p in pieces if p.strip()]


def arguments(source: str | None) -> dict[str, str]:
    """An annotation's arguments (``("/x")``, ``(path = "/x", method = ...)``) by element:
    their source, a lone value's as ``value``."""
    text = (source or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    found: dict[str, str] = {}
    for piece in _split(text):
        named = re.match(r"^([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", piece, re.DOTALL)
        if named is not None:
            found[named[1]] = named[2]
        else:
            found.setdefault("value", piece)
    return found


def _names(value: Value | None) -> list[str]:
    """The names an argument lists (``RequestMethod.GET``, ``{GET, POST}``)."""
    match value:
        case Name(dotted):
            return [dotted]
        case Array(items):
            return [name for item in items for name in _names(item)]
        case _:
            return []


# ---------------------------------------------------------------- the servlet's prefix


@dataclass(frozen=True, slots=True)
class ServletPrefix:
    path: str = ""  # the default profile's: every URL has it
    profiles: tuple[tuple[str, str], ...] = ()  # (profile, prefix) only a profile gives
    unresolved: tuple[str, ...] = ()  # a placeholder with no default


def _relaxed(key: str) -> str:
    return re.sub(r"[-_.\s]", "", key.lower())


def _flatten(data: Any) -> list[tuple[str, Any]]:
    """A YAML document's settings as dotted keys, in order. A loop, with a budget: an alias
    is the mapping it names (shared, not copied), so a mapping that holds itself is skipped
    where it recurs and aliases of aliases stop at :data:`MAX_SETTINGS` rather than multiply."""
    found: list[tuple[str, Any]] = []
    stack: list[tuple[str, Any, frozenset[int]]] = [("", data, frozenset())]
    while stack and len(found) < MAX_SETTINGS:
        above, value, around = stack.pop()
        if not isinstance(value, dict):
            found.append((above, value))
            continue
        mapping = cast("dict[Any, Any]", value)
        if id(mapping) in around:
            continue  # a: &a {b: *a}
        inside = around | {id(mapping)}
        items = list(mapping.items())
        for key, child in reversed(items):
            stack.append((f"{above}.{key}" if above else str(key), child, inside))
    return found


def _properties(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped[0] in "#!":
            continue
        matched = re.match(r"^([^=:\s]+)\s*[=:\s]\s*(.*)$", stripped)
        if matched is not None:
            found[_relaxed(matched[1])] = matched[2].strip()
    return found


def _documents(path: str, text: str) -> list[tuple[str, dict[str, str]]]:
    """A config file's settings (relaxed keys) by profile ("": the default's)."""
    named = _CONFIG_RE.match(basename(path))
    profile = (named[1] or "") if named else ""
    if path.endswith(".properties"):
        return [(profile, _properties(text))]
    found: list[tuple[str, dict[str, str]]] = []
    try:
        loaded = list(yaml.safe_load_all(text))
    except (yaml.YAMLError, RecursionError, ValueError):
        return found
    for document in loaded:
        settings = {
            _relaxed(k): str(v) for k, v in _flatten(document) if isinstance(v, str | int | float)
        }
        on = settings.get("springconfigactivateonprofile") or settings.get("springprofiles") or ""
        found.append((on or profile, settings))
    return found


def _resolved(value: str) -> str | None:
    """``${NAME:/api}`` as its default; None for a placeholder without one."""
    if "${" not in value:
        return value
    if any(m[1] is None for m in _PLACEHOLDER_RE.finditer(value)):
        return None
    return _PLACEHOLDER_RE.sub(lambda m: m[1] or "", value)


def config_root(path: str) -> str | None:
    """The module a Spring config file configures (None: not one, or a test's)."""
    name = basename(path)
    if not _CONFIG_RE.match(name) or "src/test/" in f"/{path}":
        return None
    folder = dirname(path)
    for tail in ("src/main/resources/config", "src/main/resources", "config"):
        if folder == tail or folder.endswith("/" + tail):
            return folder.removesuffix(tail).rstrip("/")
    return folder


def _prefix(
    profile: str, documents: Sequence[tuple[str, Mapping[str, str]]], unresolved: list[str]
) -> str | None:
    """The prefix one profile's documents set ("": the default's); None: they set none."""
    parts: list[str] = []
    seen = False
    for keys in PREFIX_SETTINGS:
        value = None
        for on, settings in documents:
            if on == profile:
                value = next((settings[k] for k in keys if k in settings), value)
        if value is None:
            continue
        seen = True
        resolved = _resolved(value)
        if resolved is None:
            unresolved.append(value)
            continue
        parts.append(resolved.strip().strip("/"))
    if not seen:
        return None
    return "/" + "/".join(p for p in parts if p) if any(parts) else ""


def servlet_prefixes(configs: Mapping[str, str]) -> dict[str, ServletPrefix]:
    """Each module's servlet prefix (see the module), by its folder."""
    by_root: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for path in sorted(configs):
        root = config_root(path)
        if root is not None:
            by_root.setdefault(root, []).extend(_documents(path, configs[path]))
    found: dict[str, ServletPrefix] = {}
    for root, documents in by_root.items():
        unresolved: list[str] = []
        default = _prefix("", documents, unresolved)
        only: list[tuple[str, str]] = []
        if default is None:  # a profile's holds only when it is active
            for profile in sorted({on for on, _ in documents if on}):
                given = _prefix(profile, documents, unresolved)
                if given:
                    only.append((profile, given))
        if default or only or unresolved:
            found[root] = ServletPrefix(default or "", tuple(only), tuple(unresolved))
    return found


def _prefix_for(path: str, prefixes: Mapping[str, ServletPrefix]) -> tuple[str, ServletPrefix]:
    """The nearest module above ``path`` with a prefix, and it (``""``, none: no prefix)."""
    best: str | None = None
    for root in prefixes:
        if (not root or path.startswith(root + "/")) and (best is None or len(root) > len(best)):
            best = root
    return (best, prefixes[best]) if best is not None else ("", ServletPrefix())


# ---------------------------------------------------------------- paths


def _spring_params(path: str) -> str:
    """``{id}``/``{id:[0-9]{2}}`` → ``{}``, ``{*rest}`` → ``{*}`` (braces in a regex kept
    together)."""
    out: list[str] = []
    i = 0
    while i < len(path):
        if path[i] != "{":
            out.append(path[i])
            i += 1
            continue
        depth, j = 0, i
        while j < len(path):
            depth += {"{": 1, "}": -1}.get(path[j], 0)
            if depth == 0:
                break
            j += 1
        out.append("{*}" if path[i + 1 : i + 2] == "*" else "{}")
        i = j + 1
    return "".join(out)


def normalize_path(path: str) -> str:
    """A Spring route's path as endpoints are keyed (see the module)."""
    segments: list[str] = []
    for raw in _spring_params(path.strip()).split("/"):
        if not raw:
            continue
        segment = {"**": "{*}", "*": "{}"}.get(raw, raw)
        segments.append(segment)
        if segment == "{*}":
            break
    return "/" + "/".join(segments)


def join(*parts: str) -> str:
    """Paths joined as Spring combines a class's and a method's: one slash between each."""
    kept = [p.strip().strip("/") for p in parts if p.strip().strip("/")]
    return "/" + "/".join(kept)


# ---------------------------------------------------------------- routes


@dataclass(slots=True)
class SpringReport:
    controllers: int = 0
    endpoints: int = 0
    mappings: int = 0  # mapping annotations read
    from_supertypes: int = 0  # ... declared on an interface or a class above the controller
    any_method: int = 0  # @RequestMapping with no method: an endpoint per method
    constants: int = 0  # paths that used a constant
    views: int = 0  # @Controller methods that return a view (not an API)
    duplicates: int = 0  # a method and path another mapping had
    unresolved: list[str] = field(default_factory=list[str])  # "path:line expression"
    context_paths: dict[str, str] = field(default_factory=dict[str, str])  # module: prefix
    profile_paths: dict[str, str] = field(default_factory=dict[str, str])  # module: profile=…
    notes: list[str] = field(default_factory=list[str])


@dataclass(slots=True)
class SpringRoutes:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    report: SpringReport


@dataclass(frozen=True, slots=True)
class _Route:
    method: str
    path: str  # as written, joined (context path, class's, method's)
    handler: str
    evidence_path: str
    evidence_line: int
    annotation: str
    controller: str  # the class's FQN
    module: str
    any_method: bool
    context_path: str
    root_path: str


class _Routes:
    def __init__(
        self, project: Project, files: Mapping[str, FileSpring], report: SpringReport
    ) -> None:
        self.index = TypeIndex({p: f.facts for p, f in files.items()})
        self.report = report
        self.constants: dict[str, str] = {
            k: v for f in files.values() for k, v in f.constants.items()
        }
        self.values: dict[str, list[str] | None] = {}  # each constant's, once worked out
        self.heights: dict[str, int] = {}  # how many constants deep each is built
        self.prefixes = servlet_prefixes(project.configs)
        for root, prefix in sorted(self.prefixes.items()):
            if prefix.path:
                report.context_paths[root or "."] = prefix.path
            for profile, given in prefix.profiles:
                report.profile_paths[root or "."] = f"{profile}={given}"
            report.notes += [
                f"{root or '.'}: no default for {value}" for value in prefix.unresolved
            ]

    # annotations

    def spring(self, declared: Declared, annotation: Annotation, package: str) -> str | None:
        """The annotation's simple name when it is Spring's (of ``package``), else None."""
        simple = annotation.name.rsplit(".", 1)[-1]
        ref = self.index.resolve(declared.path, declared.type.key, annotation.name)
        if ref is None:
            return simple if "." not in annotation.name else None
        return simple if ref.external and ref.fqn == f"{package}.{simple}" else None

    def find(
        self, declared: Declared, annotations: Sequence[Annotation], package: str, *names: str
    ) -> Annotation | None:
        return next((a for a in annotations if self.spring(declared, a, package) in names), None)

    def mapping(self, declared: Declared, annotations: Sequence[Annotation]) -> Annotation | None:
        return self.find(declared, annotations, WEB, "RequestMapping", *VERBS)

    # constants

    def field_of(self, type_key: str, name: str) -> tuple[str, Declared] | None:
        """A constant ``name`` of a type or its supertypes: its key and where it is declared."""
        declared = self.index.by_key.get(type_key)
        if declared is None:
            return None
        for ref in self.index.hierarchy(declared.ref()):
            above = self.index.by_key.get(ref.key) if not ref.external else None
            if above is None:
                continue
            for variable in above.type.fields:
                if variable.name == name and variable.key in self.constants:
                    return variable.key, above
        return None

    def constant(self, declared: Declared, dotted: str) -> tuple[str, Declared] | None:
        """Where a name written in ``declared``'s scope is a constant (see the module)."""
        head, _, last = dotted.rpartition(".")
        if head:
            ref = self.index.resolve(declared.path, declared.type.key, head)
            return self.field_of(ref.key, last) if ref is not None and not ref.external else None
        around: Declared | None = declared
        while around is not None:  # the type, then the types around it
            found = self.field_of(around.type.key, last)
            if found is not None:
                return found
            around = self.index.by_key.get(around.type.outer or "")
        for item in declared.facts.imports:  # import static a.b.Paths.API / a.b.Paths.*
            if (item.kind == "static" and item.name == last) or item.kind == "static_wildcard":
                ref = self.index.fqn(item.source, near=declared.path)
                found = self.field_of(ref.key, last) if not ref.external else None
                if found is not None:
                    return found
        return None

    def strings(
        self, declared: Declared, value: Value | None, depth: int = 0
    ) -> tuple[list[str] | None, bool]:
        """A path argument's values (None: one isn't a constant), and whether a constant was
        used."""
        if value is None or depth > MAX_DEPTH:
            return None, False
        match value:
            case Lit(text):
                return [text], False
            case Name(dotted):
                found = self.constant(declared, dotted)
                if found is None:
                    return None, False
                key, where = found
                if key not in self.values:
                    self.work_out(key, where)
                return self.values[key], True
            case Array(items):
                return self.each(declared, items, depth, joined=False)
            case Concat(parts):
                return self.each(declared, parts, depth, joined=True)

    def work_out(self, key: str, where: Declared) -> None:
        """The values of the constant ``key`` (declared in ``where``) and of the constants it is
        built from, each from its own declaration: the same whichever route reads one first. The
        ones it is built from come first, on a stack of their own, so a chain of any length is
        no deeper a call; one built more than :data:`MAX_DEPTH` constants deep is None, as is
        one built from itself."""
        stack = [(key, where)]
        pending = {key}
        read: dict[str, tuple[Value | None, list[tuple[str, Declared]]]] = {}
        while stack:
            at, declared = stack[-1]
            if at not in read:
                parsed = parse_value(self.constants[at])
                read[at] = parsed, self.built_from(declared, parsed)
            value, below = read[at]
            todo = next((b for b in below if b[0] not in self.values and b[0] not in pending), None)
            if todo is not None:
                stack.append(todo)
                pending.add(todo[0])
                continue
            stack.pop()
            pending.discard(at)
            if any(b in pending or b == at for b, _ in below):  # a cycle: never known
                self.values[at], self.heights[at] = None, MAX_DEPTH + 1
                continue
            height = 1 + max((self.heights[b] for b, _ in below), default=0)
            self.heights[at] = height
            self.values[at] = self.strings(declared, value)[0] if height <= MAX_DEPTH else None

    def built_from(self, declared: Declared, value: Value | None) -> list[tuple[str, Declared]]:
        """The constants ``value`` (written in ``declared``) names."""
        match value:
            case Name(dotted):
                found = self.constant(declared, dotted)
                return [found] if found is not None else []
            case Array(parts) | Concat(parts):
                return [found for part in parts for found in self.built_from(declared, part)]
            case _:
                return []

    def each(
        self, declared: Declared, values: Sequence[Value], depth: int, *, joined: bool
    ) -> tuple[list[str] | None, bool]:
        """An array's values one after another, or a concatenation's every combination."""
        out: list[str] = [""] if joined else []
        used = False
        for value in values:
            found, constant = self.strings(declared, value, depth + 1)
            if found is None:
                return None, used
            out = [a + b for a in out for b in found] if joined else [*out, *found]
            out = out[:MAX_PATHS]
            used = used or constant
            if any(len(path) > MAX_PATH_CHARS for path in out):
                return None, used  # constants of constants doubling: no route is that long
        return out, used

    def paths(
        self, declared: Declared, annotation: Annotation | None
    ) -> tuple[list[str] | None, str | None, bool]:
        """A mapping's paths ([""]: none given), what didn't resolve, whether a constant did."""
        if annotation is None:
            return [""], None, False
        given = arguments(annotation.arguments)
        source = given.get("path", given.get("value"))
        if source is None:
            return [""], None, False
        values, used = self.strings(declared, parse_value(source))
        return values, None if values is not None else source, used

    # controllers

    def controller(self, declared: Declared) -> tuple[bool, bool] | None:
        """Whether a class is a controller (None: not), and whether every response is a body."""
        annotations = declared.type.annotations
        if self.find(declared, annotations, WEB, "RestController") is not None:
            return True, True
        if self.find(declared, annotations, STEREOTYPE, "Controller") is None:
            return None
        return True, self.find(declared, annotations, WEB, "ResponseBody") is not None

    def methods(
        self, declared: Declared
    ) -> Iterator[tuple[Method, Declared, Annotation, Declared]]:
        """The controller's handler methods (its own, then those it inherits), each with the
        mapping it has (its own, else the one it overrides has) and where that is written."""
        own = list(self.index.hierarchy(declared.ref()))
        seen: set[tuple[str, int]] = set()
        for ref in own:
            above = self.index.by_key.get(ref.key) if not ref.external else None
            if above is None:
                continue
            for method in above.type.methods:
                signature = (method.name, len(method.parameters))
                if method.constructor or method.static or signature in seen:
                    continue
                seen.add(signature)
                found = self.inherited(own, signature)
                if found is not None:
                    yield method, above, *found

    def inherited(
        self, hierarchy: Sequence[TypeRef], signature: tuple[str, int]
    ) -> tuple[Annotation, Declared] | None:
        """The mapping of the nearest method with this signature that has one."""
        for ref in hierarchy:
            above = self.index.by_key.get(ref.key) if not ref.external else None
            if above is None:
                continue
            for candidate in above.type.methods:
                if (candidate.name, len(candidate.parameters)) != signature:
                    continue
                mapping = self.mapping(above, candidate.annotations)
                if mapping is not None:
                    return mapping, above
        return None

    def class_mapping(self, declared: Declared) -> tuple[Annotation | None, Declared]:
        for ref in self.index.hierarchy(declared.ref()):
            above = self.index.by_key.get(ref.key) if not ref.external else None
            if above is None:
                continue
            found = self.find(above, above.type.annotations, WEB, "RequestMapping")
            if found is not None:
                return found, above
        return None, declared

    def body(
        self, declared: Declared, method: Method, where: Declared, mapping: Annotation
    ) -> bool:
        if self.find(declared, method.annotations, WEB, "ResponseBody") is not None:
            return True
        for candidate in where.type.methods:  # on the interface's method
            if (
                candidate.name == method.name
                and self.mapping(where, candidate.annotations) == mapping
                and self.find(where, candidate.annotations, WEB, "ResponseBody") is not None
            ):
                return True
        returns = (method.returns or "").split("<", 1)[0].rsplit(".", 1)[-1]
        return returns in BODIES or any(
            f"<{name}" in (method.returns or "").replace(" ", "") for name in BODIES
        )

    @staticmethod
    def verbs(mapping: Annotation) -> tuple[tuple[str, ...], bool]:
        """A mapping's methods, and whether it takes every one (no ``method``)."""
        simple = mapping.name.rsplit(".", 1)[-1]
        if simple in VERBS:
            return (VERBS[simple],), False
        named = arguments(mapping.arguments).get("method")
        verbs = sorted(
            {n.rsplit(".", 1)[-1].upper() for n in _names(parse_value(named or ""))} & HTTP_METHODS
        )
        if named is None or not verbs:
            return ANY_METHODS, True
        return tuple(verbs), False

    def routes(self) -> list[_Route]:
        found: list[_Route] = []
        for key in sorted(self.index.by_key):
            declared = self.index.by_key[key]
            if declared.type.kind != "class" or declared.type.local:
                continue
            kind = self.controller(declared)
            if kind is None:
                continue
            _, all_bodies = kind
            self.report.controllers += 1
            prefix_mapping, prefix_where = self.class_mapping(declared)
            prefixes, missing, prefix_constant = self.paths(prefix_where, prefix_mapping)
            module, servlet = _prefix_for(declared.path, self.prefixes)
            for method, owner, mapping, where in self.methods(declared):
                self.report.mappings += 1
                if where.type.key != owner.type.key:
                    self.report.from_supertypes += 1
                if not all_bodies and not self.body(owner, method, where, mapping):
                    self.report.views += 1
                    continue
                paths, method_missing, constant = self.paths(where, mapping)
                if prefixes is None or paths is None:
                    self.report.unresolved.append(
                        f"{where.path}:{mapping.line} {method_missing or missing}"
                    )
                    continue
                if constant or prefix_constant:
                    self.report.constants += 1
                verbs, any_method = self.verbs(mapping)
                if any_method:
                    self.report.any_method += 1
                root = next((p for _, p in servlet.profiles), "")
                for verb in verbs:
                    for above in prefixes:
                        for below in paths:
                            found.append(
                                _Route(
                                    verb,
                                    join(servlet.path, above, below),
                                    method.key,
                                    where.path,
                                    mapping.line,
                                    mapping.name.rsplit(".", 1)[-1],
                                    declared.fqn or declared.type.qualified,
                                    module,
                                    any_method,
                                    servlet.path,
                                    root,
                                )
                            )
        return found


def endpoint_facts(
    project: Project, routes: Sequence[_Route], report: SpringReport, taken: frozenset[str]
) -> SpringRoutes:
    lines = {d.symbol_key: (p, d) for p, ds in project.definitions.items() for d in ds}
    chosen: dict[str, _Route] = {}
    for route in routes:
        key = f"api_endpoint {route.method} {normalize_path(route.path)}"
        held = chosen.get(key)
        if key in taken or held is not None:
            report.duplicates += 1
            # A mapping that names the method wins the path over one that takes every method.
            if key in taken or held is None or not held.any_method or route.any_method:
                continue
        chosen[key] = route
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for key, route in sorted(chosen.items()):
        where = lines.get(route.handler)
        path = where[0] if where else route.evidence_path
        attrs: dict[str, Any] = {
            "method": route.method,
            "path_raw": route.path,
            "path_norm": normalize_path(route.path),
            "framework": "spring",
            "source": "static",
            "handler": route.handler,
            "controller": route.controller,
            "module": route.module,
        }
        if route.any_method:
            attrs["any_method"] = True
        if route.context_path:
            attrs["context_path"] = normalize_path(route.context_path)
        if route.root_path:
            attrs["root_path"] = normalize_path(route.root_path)
        nodes.append(
            GraphNode(
                key=key,
                kind=NodeKind.API_ENDPOINT,
                name=f"{route.method} {route.path}",
                path=path,
                start_line=where[1].start_line if where else route.evidence_line,
                end_line=where[1].end_line if where else route.evidence_line,
                lang="java",
                attrs=attrs,
                owner=path,
            )
        )
        edges.append(
            GraphEdge(
                key,
                route.handler,
                EdgeType.HANDLED_BY,
                Provenance.CONTRACT,
                confidence=confidence_of(Provenance.CONTRACT),
                evidence_path=route.evidence_path,
                evidence_line=route.evidence_line,
                attrs={"source": "static", "annotation": route.annotation},
                owner=path,
            )
        )
    report.endpoints = len(nodes)
    return SpringRoutes(nodes, edges, report)


def spring_routes(
    project: Project, files: Mapping[str, FileSpring], *, taken: frozenset[str] = frozenset()
) -> SpringRoutes:
    """The controllers' endpoints and their handlers (see the module); ``files``: each Java
    file's facts, by path; ``taken``: endpoint keys another framework has (kept as they are)."""
    report = SpringReport()
    routes = _Routes(project, files, report).routes()
    report.unresolved = report.unresolved[:20]
    return endpoint_facts(project, routes, report, taken)
