"""FastAPI routes to ``ApiEndpoint`` nodes and ``HANDLED_BY`` edges to their handlers (3.8).

Two ways to know a route, the running app's first:

- **Runtime** (:func:`runtime_routes`): the app is imported in the sandbox
  (:mod:`sensei_engine.link.introspect.fastapi_routes`) and its routes are read back with their
  full paths, every ``include_router`` prefix applied, and the handler's module and qualified
  name. Certain (``runtime``, 1.0).
- **Static** (:func:`scan_routes`, :func:`static_routes`): route decorators
  (``@router.get("/x")``, ``api_route(methods=[...])``, ``add_api_route``), ``APIRouter(prefix=)``,
  and ``include_router(child, prefix=)``/``mount`` followed from file to file through the import
  tables (3.2). Prefixes may be constants: a string, a module constant, ``settings.X`` where
  ``settings = Settings()`` and the class has ``X = "/api/v1"``, or an f-string of those.
  ``import_resolved`` (0.9) for a route an app reaches; 0.5 for one on a router no app includes,
  or with a prefix that isn't a constant.

**Reconciling** (:func:`endpoint_facts`): with a runtime answer, its routes win; a static route
it doesn't have (a router included behind an ``if``, an app the sandbox couldn't import) is kept
at 0.5 with ``attrs.runtime_missing``. Without one, the static routes stand, and why the runtime
failed is the report's ``limitation``.

An app's ``root_path`` (``FastAPI(root_path="/api")``, a constant; or the running app's) is kept
on its endpoints (``attrs.root_path``): behind the proxy it names, clients call the endpoints
under it, and 3.10 matches them there too.

An endpoint's key is ``api_endpoint <METHOD> <path_norm>``: parameters become ``{}``, a
``{x:path}`` catch-all ``{*}``, and the trailing slash goes (3.10 matches client calls on it).
FastAPI's own routes (docs, the OpenAPI schema) have no handler in the repository and are left
out.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from tree_sitter import Node

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.project import Definition, Project
from sensei_engine.graph.python.imports import ModuleMap, PyFacts, Resolver
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.link.pytree import ancestors, call_args, line, string_value, walk
from sensei_engine.parse.parser import ParseResult
from sensei_engine.sandbox.deps import DependencyImages, detect
from sensei_engine.sandbox.runner import Sandbox, SandboxError, SandboxUnavailableError
from sensei_engine.snapshots import read_file

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")
STATIC_CONFIDENCE = 0.5  # a route no app reaches, or one the running app doesn't have
MARKER = "SENSEI-ROUTES "
INTROSPECT_SECONDS = 60.0
MAX_DEPTH = 16  # include_router chains followed
MAX_ROUTES = 5000
ENV_EXAMPLES = (".env.example", ".env.sample", ".env.template", ".env.dist", "example.env")
MAX_ENV = 60

_PARAM_RE = re.compile(r"\{([^{}:]+)(?::([^{}]+))?\}")
_FSTRING_FIELD_RE = re.compile(r"\{([^{}!:]+)\}")
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def normalize_path(path: str) -> str:
    """A route's path as endpoints are matched: ``{name}`` → ``{}``, a ``{name:path}``
    catch-all → ``{*}``, repeated slashes collapsed, no trailing slash (but ``/``)."""

    def param(found: re.Match[str]) -> str:
        return "{*}" if found[2] == "path" else "{}"

    normalized = re.sub(r"/{2,}", "/", _PARAM_RE.sub(param, path.strip()) or "/")
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized.rstrip("/") or "/"


# ---------------------------------------------------------------- what one file says


@dataclass(frozen=True, slots=True)
class RouterDecl:
    var: str
    app: bool  # FastAPI(...) (else APIRouter(...))
    prefix: str  # APIRouter(prefix=...), resolved when it's a constant ("" when it has none)
    prefix_expr: str | None  # the expression, when it isn't a plain string
    line: int
    root_path: str | None = None  # FastAPI(root_path=...), a plain string
    root_path_expr: str | None = None  # the expression, when it isn't one


@dataclass(frozen=True, slots=True)
class RouteDecl:
    router: str  # the decorated router as written: "router", "app", "items.router"
    methods: tuple[str, ...]
    path: str | None  # None: not a plain string
    path_expr: str | None
    handler: str | None  # the handler's symbol key
    line: int


@dataclass(frozen=True, slots=True)
class IncludeDecl:
    parent: str  # the router including, as written
    child: str  # the one included (or the app mounted), as written
    prefix: str | None
    prefix_expr: str | None
    line: int
    conditional: bool  # inside an if, a try or a loop
    mount: bool = False


@dataclass(slots=True)
class FileRoutes:
    path: str
    routers: dict[str, RouterDecl] = field(default_factory=dict[str, RouterDecl])
    routes: list[RouteDecl] = field(default_factory=list[RouteDecl])
    includes: list[IncludeDecl] = field(default_factory=list[IncludeDecl])
    factories: list[str] = field(default_factory=list[str])  # functions that build a FastAPI()
    # Constants for prefixes: NAME = "x"; obj = Cls(...); class Cls: ATTR = "x".
    strings: dict[str, str] = field(default_factory=dict[str, str])
    instances: dict[str, str] = field(default_factory=dict[str, str])
    class_strings: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])

    @property
    def empty(self) -> bool:
        return not (self.routers or self.routes or self.includes or self.factories)


class _Scanner:
    def __init__(self, result: ParseResult, definitions: Sequence[Definition]) -> None:
        self.result = result
        self.out = FileRoutes(result.path)
        self._callables = sorted(
            (d.start_line, d.end_line, d.name, d.symbol_key)
            for d in definitions
            if d.kind in ("function", "method")
        )

    def text(self, node: Node) -> str:
        return self.result.text(node)

    def handler(self, name: str, at: int) -> str | None:
        """The key of the function ``name`` whose definition spans line ``at`` (innermost)."""
        found = None
        for start, end, defined, key in self._callables:
            if start > at:
                break
            if defined == name and at <= end:
                found = key
        return found

    def run(self) -> FileRoutes:
        tree = self.result.tree
        if tree is None or not self.result.ok:
            return self.out
        root = tree.root_node
        for statement in root.named_children:
            self.module_statement(statement)
        for node in walk(root):
            if node.type == "decorated_definition":
                self.decorated(node)
            elif node.type == "call":
                self.call(node)
        return self.out

    # module level: routers, constants, instances, classes

    def module_statement(self, statement: Node) -> None:
        if statement.type == "class_definition":
            self.class_strings(statement)
            return
        if statement.type == "decorated_definition":
            inner = statement.child_by_field_name("definition")
            if inner is not None and inner.type == "class_definition":
                self.class_strings(inner)
            return
        assignment = statement.named_children[0] if statement.named_children else None
        if statement.type != "expression_statement" or assignment is None:
            return
        if assignment.type != "assignment":
            return
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if left is None or right is None or left.type != "identifier":
            return
        name = self.text(left)
        value = string_value(self.result, right)
        if value is not None:
            self.out.strings[name] = value
        elif right.type == "call":
            self.assigned_call(name, right)

    def assigned_call(self, name: str, call: Node) -> None:
        function = call.child_by_field_name("function")
        if function is None:
            return
        called = self.text(function).rsplit(".", 1)[-1]
        if called in ("FastAPI", "APIRouter"):
            _, keywords = call_args(call)
            prefix_node = keywords.get("prefix")
            prefix = string_value(self.result, prefix_node) if prefix_node is not None else ""
            root_node = keywords.get("root_path") if called == "FastAPI" else None
            root = string_value(self.result, root_node) if root_node is not None else None
            self.out.routers[name] = RouterDecl(
                var=name,
                app=called == "FastAPI",
                prefix=prefix or "",
                prefix_expr=None if prefix is not None else self.text(prefix_node or call),
                line=line(call),
                root_path=root,
                root_path_expr=self.text(root_node)
                if root_node is not None and root is None
                else None,
            )
        elif called[:1].isupper():
            self.out.instances[name] = self.text(function)

    def class_strings(self, node: Node) -> None:
        name, body = node.child_by_field_name("name"), node.child_by_field_name("body")
        if name is None or body is None:
            return
        found: dict[str, str] = {}
        for statement in body.named_children:
            assignment = statement.named_children[0] if statement.named_children else None
            if assignment is None or assignment.type != "assignment":
                continue
            left, right = (
                assignment.child_by_field_name("left"),
                assignment.child_by_field_name("right"),
            )
            value = string_value(self.result, right)
            if left is not None and left.type == "identifier" and value is not None:
                found[self.text(left)] = value
        if found:
            self.out.class_strings[self.text(name)] = found

    # routes

    def decorated(self, node: Node) -> None:
        definition = node.child_by_field_name("definition")
        if definition is None or definition.type != "function_definition":
            return
        name_node = definition.child_by_field_name("name")
        if name_node is None:
            return
        handler = self.handler(self.text(name_node), line(definition))
        for decorator in node.named_children:
            if decorator.type != "decorator":
                continue
            call = decorator.named_children[0] if decorator.named_children else None
            if call is None or call.type != "call":
                continue
            route = self.route_call(call, handler)
            if route is not None:
                self.out.routes.append(route)

    def route_call(
        self, call: Node, handler: str | None, *, endpoint_arg: bool = False
    ) -> RouteDecl | None:
        """``router.get("/x")``, ``router.api_route("/x", methods=[...])``,
        ``router.websocket("/ws")``; with ``endpoint_arg``, ``router.add_api_route("/x", f)``."""
        function = call.child_by_field_name("function")
        if function is None or function.type != "attribute":
            return None
        target = function.child_by_field_name("object")
        method = function.child_by_field_name("attribute")
        if target is None or method is None:
            return None
        verb = self.text(method)
        positional, keywords = call_args(call)
        path_node = positional[0] if positional else keywords.get("path")
        if verb in HTTP_METHODS:
            methods: tuple[str, ...] = (verb.upper(),)
        elif verb in ("api_route", "add_api_route"):
            methods = self.methods(keywords.get("methods")) or ("GET",)
        elif verb in ("websocket", "add_api_websocket_route"):
            methods = ("WS",)
        else:
            return None
        if path_node is None:
            return None
        path = string_value(self.result, path_node)
        if path is not None and path and not path.startswith("/"):
            return None  # not a route path: something else with a .get("key")
        return RouteDecl(
            router=self.text(target),
            methods=methods,
            path=path,
            path_expr=None if path is not None else self.text(path_node),
            handler=handler,
            line=line(call),
        )

    def methods(self, node: Node | None) -> tuple[str, ...]:
        if node is None or node.type not in ("list", "tuple", "set"):
            return ()
        values = [string_value(self.result, item) for item in node.named_children]
        return tuple(sorted({v.upper() for v in values if v}))

    def call(self, node: Node) -> None:
        function = node.child_by_field_name("function")
        if function is None:
            return
        if function.type == "identifier" and self.text(function) == "FastAPI":
            factory = next((p for p in ancestors(node) if p.type == "function_definition"), None)
            name = factory.child_by_field_name("name") if factory is not None else None
            if name is not None and self.text(name) not in self.out.factories:
                self.out.factories.append(self.text(name))
            return
        if function.type != "attribute":
            return
        target = function.child_by_field_name("object")
        method = function.child_by_field_name("attribute")
        if target is None or method is None:
            return
        verb = self.text(method)
        if verb in ("add_api_route", "add_api_websocket_route"):
            positional, keywords = call_args(node)
            endpoint = positional[1] if len(positional) > 1 else keywords.get("endpoint")
            name = self.text(endpoint) if endpoint is not None else ""
            route = self.route_call(node, self.handler(name, line(node)) or self.top(name))
            if route is not None:
                self.out.routes.append(route)
        elif verb in ("include_router", "mount"):
            self.include(node, self.text(target), mount=verb == "mount")

    def top(self, name: str) -> str | None:
        """A module-level function's key (an endpoint passed by name)."""
        return next((key for _, _, defined, key in self._callables if defined == name), None)

    def include(self, node: Node, parent: str, *, mount: bool) -> None:
        positional, keywords = call_args(node)
        if mount:
            prefix_node = positional[0] if positional else keywords.get("path")
            child = positional[1] if len(positional) > 1 else keywords.get("app")
        else:
            child = positional[0] if positional else keywords.get("router")
            prefix_node = keywords.get("prefix")
        if child is None or child.type not in ("identifier", "attribute"):
            return
        prefix = string_value(self.result, prefix_node) if prefix_node is not None else ""
        conditional = any(
            p.type in ("if_statement", "try_statement", "for_statement", "while_statement")
            for p in ancestors(node)
        )
        self.out.includes.append(
            IncludeDecl(
                parent=parent,
                child=self.text(child),
                prefix=prefix,
                prefix_expr=self.text(prefix_node) if prefix is None and prefix_node else None,
                line=line(node),
                conditional=conditional,
                mount=mount,
            )
        )


def scan_routes(result: ParseResult, definitions: Sequence[Definition]) -> FileRoutes:
    """The routing facts in one parsed Python file (``definitions``: its symbols)."""
    if result.language != "python":
        return FileRoutes(result.path)
    return _Scanner(result, definitions).run()


# ---------------------------------------------------------------- routes, statically


@dataclass(slots=True)
class Route:
    method: str
    path: str  # the full path
    handler: str | None  # symbol key
    source: str  # "runtime" or "static"
    evidence_path: str | None = None
    evidence_line: int | None = None
    name: str | None = None
    tags: tuple[str, ...] = ()
    operation_id: str | None = None
    reachable: bool = True  # an app includes it (static)
    conditional: bool = False  # through an include behind an if (static)
    unresolved: tuple[str, ...] = ()  # prefix expressions that aren't constants (static)
    entry: str | None = None  # the app it was read from (runtime)
    root_path: str = ""  # its app's (see the module)

    @property
    def key(self) -> tuple[str, str]:
        return self.method, normalize_path(self.path)


Router = tuple[str, str]  # (file, variable)


class _Static:
    def __init__(
        self, project: Project, files: Mapping[str, FileRoutes], facts: Mapping[str, PyFacts]
    ) -> None:
        self.project = project
        self.files = files
        self.resolver = Resolver(project, facts)
        self.definitions = {
            d.symbol_key: (path, d) for path, ds in project.definitions.items() for d in ds
        }

    def located(self, path: str, expr: str) -> tuple[str, str] | None:
        """Where ``expr`` (a name or ``module.name`` written in ``path``) is defined: (file,
        name there), through the file's import table."""
        if path in self.files and "." not in expr and self.defines(path, expr):
            return path, expr
        head, _, rest = expr.partition(".")
        target = self.resolver.table(path).get(head)
        if target is None or target.path is None:
            return None
        if rest:  # a module imported: its attribute
            if "." in rest:
                return None
            return target.path, rest
        found = self.definitions.get(target.key)
        return (target.path, found[1].name) if found is not None else None

    def defines(self, path: str, name: str) -> bool:
        f = self.files.get(path)
        return f is not None and (
            name in f.routers or name in f.strings or name in f.instances or name in f.class_strings
        )

    def router(self, path: str, expr: str) -> Router | None:
        found = self.located(path, expr)
        if found is None:
            return None
        where, name = found
        f = self.files.get(where)
        return (where, name) if f is not None and name in f.routers else None

    def constant(self, path: str, expr: str, depth: int = 0) -> str | None:
        """A prefix expression's value, when it's a constant (see the module)."""
        expr = expr.strip()
        if depth > 4:
            return None
        if expr.startswith(("f'", 'f"')):
            body = expr[2:-1]
            values = {m: self.constant(path, m, depth + 1) for m in _FSTRING_FIELD_RE.findall(body)}
            if any(v is None for v in values.values()):
                return None
            return _FSTRING_FIELD_RE.sub(lambda m: values[m[1]] or "", body)
        head, _, attr = expr.partition(".")
        found = self.located(path, head)
        f = self.files.get(found[0]) if found is not None else None
        if found is None or f is None:
            return None
        return self.attribute(found[0], f, found[1], attr) if attr else f.strings.get(found[1])

    def attribute(self, where: str, f: FileRoutes, name: str, attr: str) -> str | None:
        """``settings.X``: ``settings = Settings()`` and ``class Settings: X = "..."``."""
        cls = f.instances.get(name)
        if cls is None or "." in attr:
            return None
        located = self.located(where, cls)
        owner = self.files.get(located[0]) if located else None
        return owner.class_strings.get(cls.rsplit(".", 1)[-1], {}).get(attr) if owner else None

    def prefix(self, path: str, value: str | None, expr: str | None) -> tuple[str, str | None]:
        if value is not None:
            return value, None
        if expr is None:
            return "", None
        resolved = self.constant(path, expr)
        return (resolved, None) if resolved is not None else ("", expr)

    def routes(self) -> list[Route]:
        children: dict[Router, list[tuple[Router, IncludeDecl]]] = defaultdict(list)
        included: set[Router] = set()
        for path, f in self.files.items():
            for inc in f.includes:
                parent, child = self.router(path, inc.parent), self.router(path, inc.child)
                if parent is not None and child is not None:
                    children[parent].append((child, inc))
                    included.add(child)
        routers = {(p, v): r for p, f in self.files.items() for v, r in f.routers.items()}
        apps = [r for r, decl in routers.items() if decl.app and r not in included]
        roots = apps or [r for r in routers if r not in included]
        found: list[Route] = []
        reached: set[Router] = set()
        # (router, the prefix above it, conditional so far, unresolved so far, depth, root path)
        queue: deque[tuple[Router, str, bool, tuple[str, ...], int, str]] = deque(
            (root, "", False, (), 0, self.root_path(root[0], routers[root])) for root in roots
        )
        while queue and len(found) < MAX_ROUTES:
            router, above, conditional, unresolved, depth, root_path = queue.popleft()
            reached.add(router)
            path = router[0]
            decl = routers[router]
            own, own_expr = self.prefix(
                path, decl.prefix if decl.prefix_expr is None else None, decl.prefix_expr
            )
            missing = (*unresolved, *([own_expr] if own_expr else []))
            for route in self.files[path].routes:
                if self.router(path, route.router) != router:
                    continue
                found += self.expand(
                    route,
                    path,
                    prefix=above + own,
                    conditional=conditional,
                    unresolved=missing,
                    reachable=bool(apps),
                    root_path=root_path,
                )
            if depth >= MAX_DEPTH:
                continue
            for child, inc in children.get(router, []):
                prefix, expr = self.prefix(path, inc.prefix, inc.prefix_expr)
                queue.append(
                    (
                        child,
                        above + (own if not inc.mount else "") + prefix,
                        conditional or inc.conditional,
                        (*missing, *([expr] if expr else [])),
                        depth + 1,
                        root_path,
                    )
                )
        for router in routers.keys() - reached:  # never reached (e.g. an include in a cycle)
            path, _ = router
            decl = routers[router]
            for route in self.files[path].routes:
                if self.router(path, route.router) == router:
                    found += self.expand(
                        route,
                        path,
                        prefix=decl.prefix,
                        conditional=False,
                        unresolved=(),
                        reachable=False,
                    )
        return found

    def expand(
        self,
        route: RouteDecl,
        path: str,
        *,
        prefix: str,
        conditional: bool,
        unresolved: tuple[str, ...],
        reachable: bool,
        root_path: str = "",
    ) -> list[Route]:
        if route.path is None:
            value = self.constant(path, route.path_expr or "")
            unresolved = (*unresolved, *([] if value is not None else [route.path_expr or ""]))
            route_path = value or ""
        else:
            route_path = route.path
        return [
            Route(
                method=method,
                path=(prefix + route_path) or "/",
                handler=route.handler,
                source="static",
                evidence_path=path,
                evidence_line=route.line,
                reachable=reachable,
                conditional=conditional,
                unresolved=unresolved,
                root_path=root_path,
            )
            for method in route.methods
        ]

    def root_path(self, path: str, decl: RouterDecl) -> str:
        """An app's ``root_path``, when it's a constant ("" otherwise, or for a router)."""
        if decl.root_path is not None:
            return decl.root_path
        if decl.root_path_expr is None:
            return ""
        return self.constant(path, decl.root_path_expr) or ""


def static_routes(
    project: Project, files: Mapping[str, FileRoutes], facts: Mapping[str, PyFacts]
) -> list[Route]:
    """Every route the decorators and includes give (see the module); ``facts``: each file's
    imports (3.2)."""
    kept = {path: f for path, f in files.items() if not f.empty or f.strings or f.class_strings}
    return _Static(project, kept, facts).routes()


# ---------------------------------------------------------------- routes, at runtime


@dataclass(frozen=True, slots=True)
class AppEntry:
    root: str  # the import root: the directory on sys.path (relative to the snapshot)
    target: str  # "app.main:app", "app.factory:create_app()"
    path: str  # the file


@dataclass(slots=True)
class RuntimeRoutes:
    routes: list[Route]
    limitation: str | None = None  # why there are none (or fewer than the apps)
    notes: list[str] = field(default_factory=list[str])
    detail: str = ""


def app_entries(
    project: Project, files: Mapping[str, FileRoutes], facts: Mapping[str, PyFacts]
) -> list[AppEntry]:
    """The apps to import: every module-level ``FastAPI()`` no other app mounts, else the
    functions that build one (factories); by import root, then module."""
    modules = ModuleMap.build(project.paths, project.configs)
    static = _Static(project, files, facts)
    mounted = {
        child
        for path, f in files.items()
        for inc in f.includes
        if inc.mount and (child := static.router(path, inc.child)) is not None
    }
    entries: list[AppEntry] = []
    for path, f in sorted(files.items()):
        dotted = modules.names.get(path)
        if dotted is None:
            continue
        stem = path.rsplit(".", 1)[0].removesuffix("/__init__")
        depth = dotted.count(".") + 1
        root = "/".join(stem.split("/")[:-depth])
        targets = [
            f"{dotted}:{var}"
            for var, r in f.routers.items()
            if r.app and (path, var) not in mounted
        ]
        if not targets:
            targets = [f"{dotted}:{name}()" for name in f.factories]
        entries += [AppEntry(root, target, path) for target in targets]
    return entries


def env_placeholders(root: Path, folders: Iterable[str]) -> dict[str, str]:
    """Settings for the import: the names (and the sample values) the repository's example env
    files give, in ``folders`` (relative); a value that looks like a secret's is replaced."""
    found: dict[str, str] = {}
    for folder in folders:
        for name in ENV_EXAMPLES:
            path = f"{folder}/{name}" if folder else name
            try:
                text = read_file(root, path, max_bytes=64 * 1024).decode("utf-8", "replace")
            except (OSError, ValueError):
                continue
            for raw in text.splitlines():
                matched = _ENV_LINE_RE.match(raw)
                if matched is None or matched[1] in found or len(found) >= MAX_ENV:
                    continue
                value = matched[2].split(" #", 1)[0].strip().strip("'\"")
                secretish = re.search(r"secret|password|token|key|dsn", matched[1], re.I)
                found[matched[1]] = "placeholder" if secretish or not value else value[:200]
    return found


def _module_path(modules: ModuleMap, module: str | None) -> str | None:
    return modules.path(module) if module else None


def _handler(project: Project, path: str | None, qualname: str | None) -> str | None:
    if path is None or not qualname:
        return None
    definitions = project.definitions.get(path, ())
    exact = next((d.symbol_key for d in definitions if d.qualified_name == qualname), None)
    if exact is not None or "<locals>" not in qualname:
        return exact
    last = qualname.rsplit(".", 1)[-1]
    candidates = [d.symbol_key for d in definitions if d.name == last]
    return candidates[0] if len(candidates) == 1 else None


def _read_output(
    stdout: str, project: Project, modules: ModuleMap, out: RuntimeRoutes
) -> str | None:
    """The introspection's routes, added to ``out``; what went wrong, if it did."""
    line = next((raw for raw in reversed(stdout.splitlines()) if raw.startswith(MARKER)), None)
    if line is None:
        return "the import didn't finish"
    try:
        data: dict[str, Any] = json.loads(line[len(MARKER) :])
    except json.JSONDecodeError:
        return "unreadable output"
    out.notes += [str(n)[:500] for n in data.get("notes", [])][:20]
    for app in data.get("apps", []):
        ids: dict[str, str] = app.get("operation_ids") or {}
        for raw in app.get("routes", [])[:MAX_ROUTES]:
            path = _module_path(modules, raw.get("module"))
            if path is None:
                continue  # FastAPI's own (docs, schema) or a library's
            handler = _handler(project, path, raw.get("qualname"))
            methods = [str(m) for m in raw.get("methods") or ["GET"]]
            for method in methods:
                if method == "HEAD" and "GET" in methods:
                    continue  # Starlette adds HEAD to every GET
                out.routes.append(
                    Route(
                        method=method,
                        path=str(raw.get("path") or "/"),
                        handler=handler,
                        source="runtime",
                        evidence_path=path,
                        name=raw.get("name"),
                        tags=tuple(raw.get("tags") or ()),
                        operation_id=ids.get(f"{method} {raw.get('path')}"),
                        entry=app.get("entry"),
                        root_path=str(app.get("root_path") or ""),
                    )
                )
    return None


def routes_from_output(output: str, project: Project) -> RuntimeRoutes:
    """The routes the introspection printed (``output``: its stdout), as :func:`runtime_routes`
    reads them: for a run recorded elsewhere."""
    out = RuntimeRoutes([])
    failure = _read_output(output, project, ModuleMap.build(project.paths, project.configs), out)
    if failure is not None or not out.routes:
        out.limitation = failure or "the apps have no routes"
    return out


async def runtime_routes(
    sandbox: Sandbox,
    images: DependencyImages,
    root: Path,
    project: Project,
    entries: Sequence[AppEntry],
    *,
    time_limit: float = INTROSPECT_SECONDS,
) -> RuntimeRoutes:
    """The routes of ``entries``' apps, read from them running in the sandbox (the snapshot
    unpacked at ``root``). Never raises for a repository whose apps don't import: that's the
    limitation (and static routes stand)."""
    if not entries:
        return RuntimeRoutes([], limitation="no FastAPI app found to import")
    modules = ModuleMap.build(project.paths, project.configs)
    script = resources.files("sensei_engine.link.introspect").joinpath("fastapi_routes.py")
    source = script.read_bytes()
    by_root: dict[str, list[AppEntry]] = defaultdict(list)
    for entry in entries:
        by_root[entry.root].append(entry)
    out = RuntimeRoutes([])
    failures: list[str] = []
    for import_root, group in sorted(by_root.items()):
        try:
            prepared = await images.prepare(detect(root, import_root))
            env = env_placeholders(root, sorted({"", import_root}))
            request = json.dumps({"entries": [e.target for e in group]})
            result = await sandbox.run(
                root,
                ["python", "-W", "ignore", "-", request],
                image=prepared.image,
                workdir=import_root,
                env=env,
                stdin=source,
                time_limit=time_limit,
            )
        except (SandboxError, SandboxUnavailableError) as exc:
            failures.append(f"{import_root or '.'}: {exc}")
            continue
        if prepared.limitation:
            out.notes.append(f"{import_root or '.'}: {prepared.limitation}")
        failure = _read_output(result.stdout, project, modules, out)
        if failure is not None:
            failures.append(f"{import_root or '.'}: {failure} (exit {result.exit_code})")
            out.detail = result.stderr[-2000:]
    if not out.routes:
        why = failures or [n for n in out.notes if not n.startswith(" ")][:3]
        out.limitation = "; ".join(why or ["the apps have no routes"])[:1000]
    elif failures:
        out.notes += failures
    return out


# ---------------------------------------------------------------- facts


@dataclass(slots=True)
class RouteReport:
    endpoints: int = 0
    runtime: int = 0
    static: int = 0
    static_only: int = 0  # static routes the running app didn't have
    no_handler: int = 0
    limitation: str | None = None
    notes: list[str] = field(default_factory=list[str])


@dataclass(slots=True)
class RouteGraph:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    report: RouteReport


def endpoint_facts(
    project: Project, static: Sequence[Route], runtime: RuntimeRoutes | None
) -> RouteGraph:
    """``ApiEndpoint`` nodes and ``HANDLED_BY`` edges from the routes, reconciled (see the
    module)."""
    report = RouteReport()
    lines = {d.symbol_key: (p, d) for p, ds in project.definitions.items() for d in ds}
    chosen: dict[tuple[str, str], tuple[Route, float, Provenance]] = {}
    have_runtime = runtime is not None and bool(runtime.routes)
    if runtime is not None:
        report.limitation, report.notes = runtime.limitation, list(runtime.notes)
        for route in runtime.routes:
            chosen.setdefault(route.key, (route, 1.0, Provenance.RUNTIME))
            report.runtime += 1
    for route in static:
        report.static += 1
        if route.key in chosen:
            continue
        sure = route.reachable and not route.unresolved and not have_runtime
        confidence = confidence_of(Provenance.IMPORT_RESOLVED) if sure else STATIC_CONFIDENCE
        chosen[route.key] = (route, confidence, Provenance.IMPORT_RESOLVED)
        if have_runtime:
            report.static_only += 1
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for (method, norm), (route, confidence, provenance) in sorted(chosen.items()):
        key = f"api_endpoint {method} {norm}"
        where = lines.get(route.handler) if route.handler else None
        attrs: dict[str, Any] = {
            "method": method,
            "path_raw": route.path,
            "path_norm": norm,
            "framework": "fastapi",
            "source": route.source,
        }
        for name, value in (
            ("name", route.name),
            ("tags", list(route.tags) or None),
            ("operation_id", route.operation_id),
            ("handler", route.handler),
            ("entry", route.entry),
            ("root_path", normalize_path(route.root_path) if route.root_path else None),
            ("unresolved_prefix", list(route.unresolved) or None),
        ):
            if value is not None:
                attrs[name] = value
        if route.source == "static":
            attrs["reachable"] = route.reachable
            if route.conditional:
                attrs["conditional"] = True
            if have_runtime:
                attrs["runtime_missing"] = True
        path = where[0] if where else route.evidence_path
        nodes.append(
            GraphNode(
                key=key,
                kind=NodeKind.API_ENDPOINT,
                name=f"{method} {route.path}",
                path=path,
                start_line=where[1].start_line if where else route.evidence_line or 0,
                end_line=where[1].end_line if where else route.evidence_line or 0,
                lang="python",
                attrs=attrs,
                owner=path,
            )
        )
        if route.handler is None:
            report.no_handler += 1
            continue
        edges.append(
            GraphEdge(
                key,
                route.handler,
                EdgeType.HANDLED_BY,
                provenance,
                confidence=confidence,
                evidence_path=route.evidence_path or path,
                evidence_line=route.evidence_line or (where[1].start_line if where else None),
                attrs={"source": route.source},
                owner=path,
            )
        )
    report.endpoints = len(nodes)
    return RouteGraph(nodes, edges, report)


async def fastapi_routes(
    root: Path,
    project: Project,
    files: Mapping[str, FileRoutes],
    facts: Mapping[str, PyFacts],
    *,
    sandbox: Sandbox | None = None,
    images: DependencyImages | None = None,
) -> RouteGraph:
    """Everything above for one snapshot: static routes, the runtime's when a sandbox is given
    (and the repository has an app), reconciled into facts."""
    static = static_routes(project, files, facts)
    runtime = None
    if sandbox is not None and images is not None:
        entries = app_entries(project, files, facts)
        if entries:
            runtime = await runtime_routes(sandbox, images, root, project, entries)
    return endpoint_facts(project, static, runtime)
