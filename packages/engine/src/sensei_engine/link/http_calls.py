"""Frontend HTTP calls to ``HttpClientCall`` nodes (3.9), for JavaScript and TypeScript.

:func:`scan_calls` reads one parsed file, :func:`client_calls` joins the files (imported
constants, axios instances and wrappers) into facts.

**Detected calls:**

- ``fetch(url, {method})``;
- ``axios.get/post/...(url)``, ``axios(config)``, ``axios.request(config)``, and the same on an
  instance from ``axios.create({baseURL})``, whose base path the URL is under;
- ``ky.get(url)``, ``ky(url, {method})``;
- openapi-fetch's ``client.GET("/path")``;
- a client called with a config object: ``client.get({url: "/x"})`` (hey-api's generated SDKs,
  ``(options.client ?? client).post({...})``);
- SWR's ``useSWR("/api/x", fetcher)``;
- calls to a **wrapper**: a function whose parameter goes into a detected call's URL
  (``function get(path) { return fetch(BASE + path) }``), followed up to
  :data:`MAX_WRAPPER_DEPTH` wrappers deep.

React Query's ``useQuery({queryFn})``/``useMutation({mutationFn})`` need nothing of their own: the
call inside the function is found where it is, in the component or hook around it.

**URLs** (``url_template``): string literals, template literals (a value that isn't a constant
becomes ``{}``), ``+`` concatenation, ``a ?? b``/``a || b`` (the first that's known), constants
of the file or imported from another (resolved as 3.3 resolves imports), properties of a
constant object, ``import.meta.env.X``/``process.env.X`` (the value in the project's ``.env``
files, else an ``{env:X}`` segment), and calls to a **URL helper**, a function of the file or
imported that returns a URL (``getURL("API_KEY")``, ``getBaseUrl()``), evaluated with the call's
arguments (:mod:`sensei_engine.link.url_helpers`). A URL with nothing literal in it is
``dynamic``.

**Not calls:** a ``.get("/x")`` on what is plainly not an HTTP client (a ``Map``,
``URLSearchParams``, ``headers``, ``localStorage``), and anything in a server file (one
importing ``express``, ``koa``, ``fastify`` or ``hono``, whose ``app.get("/x")`` is a route).

**Operations:** a call in a generated SDK's method (a ``*.gen.ts`` file, as hey-api and
openapi-typescript-codegen write them) names its operation in the method's options type:
``loginAccessToken(options: Options<loginLoginAccessTokenData>)`` is the operation
``loginLoginAccessToken``, the generator's form of the operationId ``login-login_access_token``
(3.10 links it to the endpoint with that operationId).

Facts: an ``HttpClientCall`` node per call (``http_client_call <METHOD> <url_template>
<path>:<line>``; attrs ``method``, ``url_raw``, ``url_template``, ``base_ref``, ``dynamic``,
``client``, ``operation_id`` for a generated one) and ``ISSUES_REQUEST`` from the innermost
definition around it (``contract``: the call is in its body), plus an :class:`HttpCall` per call
for 3.10.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from posixpath import dirname

from tree_sitter import Node

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.project import Definition, Project
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.imports.ecma.config import ProjectConfig
from sensei_engine.imports.ecma.resolve import ModuleResolver, ResolutionKind
from sensei_engine.link import url_helpers
from sensei_engine.link.pytree import ancestors, walk
from sensei_engine.link.url_helpers import (
    Call,
    Choice,
    Concat,
    Either,
    Expr,
    Flag,
    Helper,
    HelperCall,
    KeyValue,
    Lookup,
    ObjectLiteral,
    Param,
    PerKey,
    Text,
    Undefined,
)
from sensei_engine.parse.parser import ParseResult

ECMA_LANGUAGES = ("javascript", "jsx", "typescript", "tsx")
HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")
MAX_WRAPPER_DEPTH = 3
MAX_RESOLVE_DEPTH = 8  # imports of imports, helpers calling helpers
MAX_URL = 500
ENV_FILES = (".env", ".env.development", ".env.local", ".env.example", ".env.sample")
HOLE = "{}"

_NOT_CLIENTS = re.compile(
    r"(?:^|\.)(?:params|searchParams|query|headers|formData|form|map|cache|store|storage|"
    r"localStorage|sessionStorage|cookies?|router|app|server|express|weakMap|set|url)$",
    re.IGNORECASE,
)
_SERVER_PACKAGES = frozenset({"express", "koa", "fastify", "hono", "@hono/node-server"})
_FUNCTIONS = frozenset({
    "function_declaration", "function_expression", "arrow_function", "method_definition",
    "function", "generator_function_declaration",
})  # fmt: skip


@dataclass(frozen=True, slots=True)
class _ExprContext:
    """Where a helper's expression is read: ``caller``, a call's argument (the parameters are
    then the calling function's, not known at the call); ``extended``, the helper's local
    variables extended per key (:func:`_extended`); ``key``, inside one's extension, its loop
    variable."""

    caller: bool = False
    extended: Mapping[str, tuple[int, Node, str]] = field(
        default_factory=dict[str, tuple[int, Node, str]]
    )
    key: str | None = None


_PLAIN = _ExprContext()
_CALLER = _ExprContext(caller=True)
_PARAM_TOKEN = "\x00p{}\x00"  # noqa: S105 - a wrapper's parameter inside a URL template
_MARK = "\x00p"
_MARK_RE = re.compile(r"\x00p(\d+)\x00")
_PLACEHOLDER_RE = re.compile(r"\{(?:import|helper):[^}]*\}")


@dataclass(frozen=True, slots=True)
class HttpCall:
    """A call for 3.10 to link to an endpoint (:mod:`sensei_engine.link.urls`)."""

    key: str  # its node key
    method: str
    url: str  # the URL template: "/api/items/{}", "{env:VITE_API_URL}/items", "https://x/y"
    base: str = ""  # the base path resolved for it (an axios instance's baseURL), if any
    dynamic: bool = False  # the URL couldn't be worked out (a variable)
    operation_id: str | None = None  # a generated client's operation
    evidence_path: str | None = None
    evidence_line: int | None = None
    owner: str | None = None


@dataclass(frozen=True, slots=True)
class CallSite:
    line: int
    caller: str  # the innermost definition's key (the module's at top level)
    method: str
    url: str | None  # the template, with {} holes and {env:X}; None: nothing known
    url_raw: str
    client: str  # fetch, axios, ky, swr, openapi-fetch, config
    instance: str | None = None  # the axios instance's name, as written (its base applies)
    wrapper_args: tuple[str | None, ...] = ()  # (a call to a wrapper) its URL-ish arguments
    wrapper: str | None = None  # (a call to a wrapper) the wrapper's name, as written
    operation: str | None = None  # a generated SDK's operation (see the module)


@dataclass(frozen=True, slots=True)
class Wrapper:
    """A function passing a parameter into a call's URL: calls to it are calls."""

    name: str
    key: str
    param: int  # which parameter is the URL's
    template: str  # the call's URL, the parameter's place marked with {param}
    method: str
    client: str
    instance: str | None = None


@dataclass(frozen=True, slots=True)
class PendingWrapper:
    """A function passing a parameter on to another wrapper (``get(p) -> request("GET", p)``):
    a wrapper itself once that one is known."""

    name: str
    key: str
    inner: str  # the wrapper it calls, as written
    args: tuple[str | None, ...]  # that call's URL-ish arguments, parameters marked


@dataclass(slots=True)
class FileCalls:
    path: str
    imports: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    constants: dict[str, str] = field(default_factory=dict[str, str])  # NAME -> URL template
    objects: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    instances: dict[str, str] = field(default_factory=dict[str, str])  # api -> its baseURL
    calls: list[CallSite] = field(default_factory=list[CallSite])
    wrappers: dict[str, Wrapper] = field(default_factory=dict[str, Wrapper])
    pending: list[PendingWrapper] = field(default_factory=list[PendingWrapper])
    helpers: dict[str, Helper] = field(default_factory=dict[str, Helper])  # URL helpers
    helper_calls: list[HelperCall] = field(default_factory=list[HelperCall])  # {helper:N}'s
    default: str | None = None  # the name ``export default`` exports, when it names one
    server: bool = False

    @property
    def empty(self) -> bool:
        return not (
            self.calls
            or self.constants
            or self.objects
            or self.instances
            or self.wrappers
            or self.helpers
        )


def _text(node: Node | None) -> str:
    raw = node.text if node is not None else None
    return raw.decode("utf-8", "replace") if raw else ""


def _unwrap(node: Node) -> Node:
    """Through parentheses, ``as`` and ``!``."""
    while (
        node.type
        in (
            "parenthesized_expression",
            "as_expression",
            "non_null_expression",
            "satisfies_expression",
            "type_assertion",
        )
        and node.named_children
    ):
        node = node.named_children[0]
    return node


def _string(node: Node) -> str | None:
    if node.type != "string":
        return None
    return "".join(
        _text(c) if c.type == "string_fragment" else _escape(_text(c)) for c in node.named_children
    )


def _escape(text: str) -> str:
    return text[1:] if text.startswith("\\") and len(text) == 2 else text


def _object(node: Node) -> dict[str, Node]:
    """An object literal's plain properties (``{url: x, "method": y}``)."""
    found: dict[str, Node] = {}
    if node.type != "object":
        return found
    for pair in node.named_children:
        if pair.type == "pair":
            key, value = pair.child_by_field_name("key"), pair.child_by_field_name("value")
            if key is not None and value is not None:
                name = _string(key) if key.type == "string" else _text(key)
                if name:
                    found[name] = value
        elif pair.type == "shorthand_property_identifier":
            found[_text(pair)] = pair
    return found


class _Scanner:
    def __init__(
        self, result: ParseResult, definitions: Sequence[Definition], env: Mapping[str, str]
    ) -> None:
        self.result = result
        self.env = env
        self.out = FileCalls(result.path)
        self._module_objects: dict[str, Node] = {}  # the module's object constants, as written
        module = next((d for d in definitions if d.kind == "module"), None)
        self.module_key = module.symbol_key if module else f"{result.language} {result.path}/"
        self._scopes = sorted(
            (d.start_line, d.end_line, d.symbol_key, d.name)
            for d in definitions
            if d.kind in ("function", "method", "component", "hook")
        )

    def caller(self, line: int) -> str:
        found = self.module_key
        for start, end, key, _ in self._scopes:
            if start > line:
                break
            if line <= end:
                found = key
        return found

    def run(self) -> FileCalls:
        tree = self.result.tree
        if tree is None or not self.result.ok:
            return self.out
        root = tree.root_node
        for node in root.named_children:
            self.top_level(node)
        if self.out.server:
            return self.out
        for node in walk(root):
            if node.type == "call_expression":
                self.call(node)
        return self.out

    # module level

    def top_level(self, node: Node) -> None:
        if node.type == "import_statement":
            self.import_statement(node)
            return
        if node.type == "export_statement":
            inner = node.child_by_field_name("declaration")
            if inner is not None:
                self.top_level(inner)
            if any(c.type == "default" for c in node.children):
                # `export default name;`, `export default function name() {}`
                value = node.child_by_field_name("value")
                named = inner.child_by_field_name("name") if inner is not None else value
                if named is not None and named.type == "identifier":
                    self.out.default = _text(named)
            return
        if node.type == "function_declaration":
            name = node.child_by_field_name("name")
            if name is not None:
                self.helper(node, _text(name))
            return
        if node.type in ("lexical_declaration", "variable_declaration"):
            for declarator in node.named_children:
                if declarator.type == "variable_declarator":
                    self.declarator(declarator)

    def import_statement(self, node: Node) -> None:
        source = node.child_by_field_name("source")
        module = _string(source) if source is not None else None
        if not module:
            return
        if module in _SERVER_PACKAGES:
            self.out.server = True
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        for part in clause.named_children if clause is not None else ():
            if part.type == "identifier":
                self.out.imports[_text(part)] = (module, "default")
            elif part.type == "named_imports":
                for spec in part.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name, alias = (
                        spec.child_by_field_name("name"),
                        spec.child_by_field_name("alias"),
                    )
                    self.out.imports[_text(alias or name)] = (module, _text(name))
            elif part.type == "namespace_import":
                ident = next((c for c in part.named_children if c.type == "identifier"), None)
                if ident is not None:
                    self.out.imports[_text(ident)] = (module, "*")

    def declarator(self, node: Node) -> None:
        name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
        if name is None or value is None or name.type != "identifier":
            return
        value = _unwrap(value)
        if value.type == "object":
            self._module_objects[_text(name)] = value
            props = {k: self.url(v) for k, v in _object(value).items()}
            self.out.objects[_text(name)] = {k: v for k, v in props.items() if v is not None}
            return
        if value.type in ("arrow_function", "function_expression", "function"):
            self.helper(value, _text(name))
            return
        if value.type == "call_expression":
            function = value.child_by_field_name("function")
            if function is not None and _text(function).endswith(".create"):
                args, _ = self.arguments(value)
                config = _object(_unwrap(args[0])) if args else {}
                base = config.get("baseURL") or config.get("prefixUrl") or config.get("baseUrl")
                self.out.instances[_text(name)] = (self.url(base) or "") if base else ""
                return
        url = self.url(value)
        if url is not None and _looks_like_url(url):
            self.out.constants[_text(name)] = url

    # URLs

    def url(self, node: Node | None, params: Sequence[str] = (), depth: int = 0) -> str | None:
        """The template a URL expression gives (see the module); ``params``: the enclosing
        function's parameter names, marked for wrappers."""
        if node is None or depth > 8:
            return None
        node = _unwrap(node)
        if node.type == "string":
            return _string(node)
        if node.type == "member_expression":
            return self.member(node)
        if node.type == "call_expression":
            return self.helper_call(node, params, depth)
        readers = {
            "template_string": self.template,
            "binary_expression": self.binary,
            "identifier": self.identifier,
            "ternary_expression": self.ternary,
        }
        reader = readers.get(node.type)
        return reader(node, params, depth) if reader is not None else None

    def template(self, node: Node, params: Sequence[str], depth: int) -> str:
        parts: list[str] = []
        for child in node.named_children:
            if child.type == "string_fragment":
                parts.append(_text(child))
            elif child.type == "escape_sequence":
                parts.append(_escape(_text(child)))
            elif child.type == "template_substitution":
                inner = child.named_children[0] if child.named_children else None
                value = self.url(inner, params, depth + 1)
                parts.append(HOLE if value is None else value)
        return "".join(parts)

    def ternary(self, node: Node, params: Sequence[str], depth: int) -> str | None:
        """``a ? b : c`` in a URL: the path both branches share once their query strings are
        cut (`${qs ? `?${qs}` : ""}` is none, ``qs ? `${base}?${qs}` : base`` is ``base``);
        else not known."""
        then = self.url(node.child_by_field_name("consequence"), params, depth + 1)
        otherwise = self.url(node.child_by_field_name("alternative"), params, depth + 1)
        if then is None or otherwise is None:
            return None
        cut = [re.split(r"[?#]", branch, maxsplit=1)[0] for branch in (then, otherwise)]
        return cut[0] if cut[0] == cut[1] else None

    def binary(self, node: Node, params: Sequence[str], depth: int) -> str | None:
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        operator = node.child_by_field_name("operator")
        op = _text(operator)
        if op in ("??", "||"):
            return self.url(left, params, depth + 1) or self.url(right, params, depth + 1)
        if op != "+":
            return None
        a, b = self.url(left, params, depth + 1), self.url(right, params, depth + 1)
        if a is None and b is None:
            return None
        return (a if a is not None else HOLE) + (b if b is not None else HOLE)

    def identifier(self, node: Node, params: Sequence[str], depth: int) -> str | None:
        name = _text(node)
        if name in params:
            return _PARAM_TOKEN.replace("{}", str(list(params).index(name)))
        local = self.local(node, name)
        if local is not None:
            return self.url(local, params, depth + 1)
        if name in self.out.constants:
            return self.out.constants[name]
        if name in self.out.imports:
            return "{import:" + name + "}"  # resolved when the files are joined
        return None

    @staticmethod
    def local(node: Node, name: str) -> Node | None:
        """The value of a ``const``/``let`` named ``name`` declared in a block around ``node``
        (before it), innermost first."""
        line = node.start_point.row
        for block in ancestors(node):
            if block.type not in ("statement_block", "program"):
                continue
            if block.type == "program":
                return None  # module level: the constants
            for statement in block.named_children:
                if statement.start_point.row >= line:
                    break
                if statement.type not in ("lexical_declaration", "variable_declaration"):
                    continue
                for declarator in statement.named_children:
                    if (
                        declarator.type == "variable_declarator"
                        and _text(declarator.child_by_field_name("name")) == name
                    ):
                        return declarator.child_by_field_name("value")
        return None

    def member(self, node: Node) -> str | None:
        text = _text(node)
        env = re.fullmatch(r"(?:import\.meta\.env|process\.env)\.([A-Za-z_][A-Za-z0-9_]*)", text)
        if env is not None:
            value = self.env.get(env[1])
            return value if value is not None else "{env:" + env[1] + "}"
        head, _, prop = text.rpartition(".")
        if head in self.out.objects and prop in self.out.objects[head]:
            return self.out.objects[head][prop]
        if head in self.out.imports and "." not in head:
            return "{import:" + text + "}"
        return None

    # URL helpers (see :mod:`sensei_engine.link.url_helpers`)

    def helper(self, node: Node, name: str) -> None:
        """``node``, a function named ``name``, as a URL helper when it returns a URL from one
        ``return`` (or an arrow's expression): its value over its parameters, each local
        variable it extends per key of an object parameter followed, any other reassignment of
        one making it no helper."""
        params, defaults = self.helper_params(node)
        body = node.child_by_field_name("body")
        if body is None:
            return
        if body.type != "statement_block":
            value: Node | None = body
        else:
            returns = [n for n in _own(body) if n.type == "return_statement"]
            if len(returns) != 1 or not returns[0].named_children:
                return
            value = returns[0].named_children[0]
        extended = _extended(body, params)
        if extended is None:
            return
        found = self.expr(value, params, context=_ExprContext(extended=extended))
        if found is not None and (url_helpers.urlish(found) or url_helpers.passes_through(found)):
            self.out.helpers[name] = Helper(name, found, defaults)

    def helper_params(self, node: Node) -> tuple[list[str], tuple[Expr | None, ...]]:
        """A function's parameter names, and their defaults (``= false``, ``= {}``)."""
        names: list[str] = []
        defaults: list[Expr | None] = []
        params = node.child_by_field_name("parameters")
        if params is None:
            single = node.child_by_field_name("parameter")
            return ([_text(single)], (None,)) if single is not None else ([], ())
        for param in params.named_children:
            if param.type == "comment":
                continue
            pattern = param.child_by_field_name("pattern") or param.child_by_field_name("left")
            default = param.child_by_field_name("value") or param.child_by_field_name("right")
            ident = param if param.type == "identifier" else pattern
            names.append(_text(ident) if ident is not None and ident.type == "identifier" else "")
            defaults.append(self.expr(default, ()) if default is not None else None)
        return names, tuple(defaults)

    def helper_call(self, node: Node, params: Sequence[str], depth: int) -> str | None:
        """A call in a URL: a ``{helper:N}`` placeholder the join evaluates (a hole when the
        function turns out to be no URL helper)."""
        function = _callee(node)
        if function is None or function.type not in ("identifier", "member_expression"):
            return None
        if depth > 8:
            return None
        args, _ = self.arguments(node)
        values = tuple(self.expr(a, params, context=_CALLER) for a in args[:6])
        self.out.helper_calls.append(HelperCall(_text(function), values))
        return "{helper:" + str(len(self.out.helper_calls) - 1) + "}"

    def expr(  # noqa: PLR0911, PLR0912 - one case per kind of expression
        self,
        node: Node | None,
        params: Sequence[str],
        depth: int = 0,
        context: _ExprContext | None = None,
    ) -> Expr | None:
        """``node`` as a helper's expression over ``params`` (see
        :mod:`~sensei_engine.link.url_helpers`), in ``context`` (:class:`_ExprContext`)."""
        if node is None or depth > 8:
            return None
        node = _unwrap(node)
        context = context or _PLAIN

        def again(inner: Node | None) -> Expr | None:
            return self.expr(inner, params, depth + 1, context)

        kind = node.type
        if kind == "string":
            text = _string(node)
            return Text(text) if text is not None else None
        if kind in ("true", "false"):
            return Flag(kind == "true")
        if kind == "undefined":
            return Undefined()
        if kind == "template_string":
            parts: list[Expr | None] = []
            for child in node.named_children:
                if child.type == "string_fragment":
                    parts.append(Text(_text(child)))
                elif child.type == "escape_sequence":
                    parts.append(Text(_escape(_text(child))))
                elif child.type == "template_substitution":
                    parts.append(again(child.named_children[0] if child.named_children else None))
            return Concat(tuple(parts))
        if kind == "binary_expression":
            op = _text(node.child_by_field_name("operator"))
            left = again(node.child_by_field_name("left"))
            right = again(node.child_by_field_name("right"))
            if op in ("??", "||"):
                return Either(left, right)
            if op == "+" and (left is not None or right is not None):
                return Concat((left, right))
            return None
        if kind == "ternary_expression":
            condition = _unwrap_opt(node.child_by_field_name("condition"))
            then = again(node.child_by_field_name("consequence"))
            otherwise = again(node.child_by_field_name("alternative"))
            if then is not None and otherwise is not None:
                # The path both share once their query strings are cut (`${qs ? `?${qs}` : ""}`,
                # ``qs ? `${base}?${qs}` : base``): a query string is never a path segment.
                path, other = _no_query(then), _no_query(otherwise)
                if path == other:
                    return path
            name = _text(condition) if condition is not None else ""
            if condition is not None and condition.type == "identifier" and name in params:
                if context.caller:
                    return None  # the calling function's parameter: not known at the call
                return Choice(list(params).index(name), then, otherwise)
            return then if then == otherwise else None
        if kind == "object":
            return ObjectLiteral(tuple((k, again(v)) for k, v in _object(node).items()))
        if kind == "identifier":
            return self.expr_name(node, params, depth, context)
        if kind == "member_expression":
            value = self.member(node)
            return Text(value) if value is not None else None
        if kind == "subscript_expression":
            return self.expr_subscript(node, params, context.key)
        if kind == "call_expression":
            function = _callee(node)
            if function is None or function.type not in ("identifier", "member_expression"):
                return None
            args, _ = self.arguments(node)
            return Call(_text(function), tuple(again(a) for a in args[:6]))
        return None

    def expr_name(  # noqa: PLR0911 - one return per kind of name
        self, node: Node, params: Sequence[str], depth: int, context: _ExprContext
    ) -> Expr | None:
        name = _text(node)
        if name == "undefined":
            return Undefined()
        if name in params:
            return None if context.caller else Param(list(params).index(name))
        local = self.local(node, name)
        if local is not None:
            value = self.expr(local, params, depth + 1, context)
            if name in context.extended:
                param, each, loop = context.extended[name]
                inner = _ExprContext(key=loop)
                return PerKey(value, param, self.expr(each, params, depth + 1, inner))
            return value
        if name in self.out.constants:
            return Text(self.out.constants[name])
        if name in self.out.imports:
            return Text("{import:" + name + "}")
        return None

    def expr_subscript(self, node: Node, params: Sequence[str], key: str | None) -> Expr | None:
        """``URLs[key]`` (a constant object by a parameter), ``params[k]`` (inside an
        extension, the value of the key it is at), ``URLs["X"]``."""
        table = _unwrap_opt(node.child_by_field_name("object"))
        index = _unwrap_opt(node.child_by_field_name("index"))
        if table is None or index is None or table.type != "identifier":
            return None
        name = _text(table)
        if index.type == "identifier" and key is not None and _text(index) == key:
            return KeyValue()
        known = name in self.out.objects or name in self.out.imports
        if index.type == "identifier" and _text(index) in params and known:
            return Lookup(name, list(params).index(_text(index)))
        literal = _string(index) if index.type == "string" else None
        if literal is not None and name in self.out.objects:
            value = self.out.objects[name].get(literal)
            return Text(value) if value is not None else None
        return None

    # calls

    def arguments(self, call: Node) -> tuple[list[Node], dict[str, Node]]:
        args = call.child_by_field_name("arguments")
        if args is None:
            return [], {}
        positional = [a for a in args.named_children if a.type != "comment"]
        return positional, {}

    def function_params(self, node: Node) -> tuple[list[str], Node | None]:
        """The parameter names of the function around ``node``, and that function."""
        for parent in ancestors(node):
            if parent.type in (
                "function_declaration",
                "function_expression",
                "arrow_function",
                "method_definition",
                "function",
                "generator_function_declaration",
            ):
                params = parent.child_by_field_name("parameters")
                names: list[str] = []
                if params is not None:
                    for p in params.named_children:
                        ident = (
                            p
                            if p.type == "identifier"
                            else next((c for c in p.named_children if c.type == "identifier"), None)
                        )
                        names.append(_text(ident) if ident is not None else "")
                else:
                    single = parent.child_by_field_name("parameter")
                    if single is not None:
                        names.append(_text(single))
                return names, parent
        return [], None

    def call(self, node: Node) -> None:
        function = _callee(node)
        if function is None:
            return
        if function.type == "identifier":
            self.plain_call(node, _text(function))
        elif function.type == "member_expression":
            target = function.child_by_field_name("object")
            prop = function.child_by_field_name("property")
            if target is not None and prop is not None:
                self.member_call(node, _text(_unwrap(target)), _text(prop))

    def plain_call(self, node: Node, name: str) -> None:
        args, _ = self.arguments(node)
        params, around = self.function_params(node)
        if name == "fetch":
            self.found(node, args, params, around, client="fetch", options=1)
        elif name in ("axios", "ky") and self.imported_from(name, name):
            self.found(node, args, params, around, client=name, options=1, config_first=True)
        elif name in ("useSWR", "useSWRImmutable"):
            self.found(node, args[:1], params, around, client="swr", options=-1)
        else:
            self.maybe_wrapper_call(node, name, args)

    def member_call(self, node: Node, receiver: str, verb: str) -> None:
        args, _ = self.arguments(node)
        params, around = self.function_params(node)
        if verb == "fetch" and receiver in ("window", "globalThis", "self"):
            self.found(node, args, params, around, client="fetch", options=1)
            return
        if verb.lower() not in HTTP_VERBS and verb != "request":
            if verb[:1].islower():
                self.maybe_wrapper_call(node, f"{receiver}.{verb}", args)
            return
        if _NOT_CLIENTS.search(receiver.rsplit("??", maxsplit=1)[-1].strip(" ()")):
            return
        client = "openapi-fetch" if verb.isupper() else self.client_of(receiver)
        known = receiver in self.out.instances or receiver in self.out.imports
        self.found(
            node,
            args,
            params,
            around,
            client=client,
            method=None if verb == "request" else verb.upper(),
            options=1,
            config_first=True,
            instance=receiver if known else None,
            strict=client == "config",
        )

    def imported_from(self, name: str, package: str) -> bool:
        source = self.out.imports.get(name)
        return source is not None and source[0] == package

    def client_of(self, receiver: str) -> str:
        if receiver == "axios" or receiver in self.out.instances:
            return "axios"
        if receiver == "ky":
            return "ky"
        return "config"

    def found(
        self,
        node: Node,
        args: Sequence[Node],
        params: Sequence[str],
        around: Node | None,
        *,
        client: str,
        options: int,
        method: str | None = "GET",
        config_first: bool = False,
        instance: str | None = None,
        strict: bool = False,
    ) -> None:
        """A detected call: its URL from the first argument (or the config object), its method
        from the options (or ``method``)."""
        if not args:
            return
        first = _unwrap(args[0])
        config: dict[str, Node] = {}
        if config_first and first.type == "object":
            config = _object(first)
            url_node = config.get("url")
        else:
            url_node = first
            if 0 < options < len(args):
                config = self.config_object(args[options])
        if url_node is None:
            return
        url = self.url(url_node, params)
        method_node = config.get("method")
        if method_node is not None:
            value = self.url(method_node)
            method = value.upper() if value else None
        line = node.start_point.row + 1
        if url is not None and _MARK in url and around is not None:
            self.wrapper(around, url, method or "GET", client, instance)
            return
        if strict and (url is None or not _looks_like_url(url)):
            return  # a .get({...}) on something unknown, without a URL in it: not a call
        if client == "config" and url is not None and not _looks_like_url(url):
            return
        self.out.calls.append(
            CallSite(
                line=line,
                caller=self.caller(line),
                method=method or "GET",
                url=url,
                url_raw=_text(url_node)[:MAX_URL],
                client=client,
                instance=instance,
                operation=self.operation(around),
            )
        )

    def config_object(self, node: Node) -> dict[str, Node]:
        """A call's options: an object literal, or a ``const`` holding one (of the function
        around the call, or of the module): ``fetch(url, options)``."""
        node = _unwrap(node)
        if node.type == "identifier":
            name = _text(node)
            local = self.local(node, name)
            found = _unwrap(local) if local is not None else self._module_objects.get(name)
            return _object(found) if found is not None and found.type == "object" else {}
        return _object(node)

    def operation(self, around: Node | None) -> str | None:
        """The operation a generated SDK's method is for: its options type's ``<op>Data``."""
        if around is None or not _GENERATED_RE.search(self.result.path):
            return None
        params = around.child_by_field_name("parameters")
        first = next(
            (p for p in (params.named_children if params else []) if p.type != "comment"), None
        )
        annotation = first.child_by_field_name("type") if first is not None else None
        found = _OPERATION_RE.search(_text(annotation)) if annotation is not None else None
        return found[1] if found else None

    @staticmethod
    def function_name(around: Node) -> str:
        """A function's name: its own, or the variable it's assigned to."""
        name_node = around.child_by_field_name("name")
        if name_node is not None:
            return _text(name_node)
        parent = around.parent
        if parent is not None and parent.type == "variable_declarator":
            return _text(parent.child_by_field_name("name"))
        return ""

    def wrapper(
        self, around: Node, url: str, method: str, client: str, instance: str | None
    ) -> None:
        name = self.function_name(around)
        if not name:
            return
        found = _MARK_RE.search(url)
        if found is None:
            return
        line = around.start_point.row + 1
        self.out.wrappers[name] = Wrapper(
            name=name,
            key=self.caller(line),
            param=int(found[1]),
            template=_MARK_RE.sub("{param}", url),
            method=method,
            client=client,
            instance=instance,
        )

    def maybe_wrapper_call(self, node: Node, name: str, args: Sequence[Node]) -> None:
        """A call that may be to a wrapper (known once every file is read)."""
        if not args:
            return
        params, around = self.function_params(node)
        values = tuple(self.url(a, params) for a in args[:4])
        line = node.start_point.row + 1
        marked = any(v is not None and _MARK in v for v in values)
        if marked and around is not None and self.function_name(around):
            key = self.caller(around.start_point.row + 1)
            self.out.pending.append(PendingWrapper(self.function_name(around), key, name, values))
            return
        if not any(v is not None and _looks_like_url(v) for v in values):
            return
        self.out.calls.append(
            CallSite(
                line=line,
                caller=self.caller(line),
                method="GET",
                url=None,
                url_raw=_text(args[0])[:MAX_URL],
                client="wrapper",
                wrapper=name,
                wrapper_args=values,
            )
        )


# A generated client's file, and the operation in its options type (``Options<xData, ...>``).
_GENERATED_RE = re.compile(r"\.gen\.[cm]?[jt]sx?$")
_OPERATION_RE = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*?)Data\b")


def _callee(call: Node) -> Node | None:
    """What a call calls, through parentheses and casts, and through the ``await`` tree-sitter's
    TypeScript puts around it when the call has type arguments (``await api.get<T>(url)``)."""
    function = call.child_by_field_name("function")
    if function is None:
        return None
    function = _unwrap(function)
    if function.type == "await_expression" and function.named_children:
        function = _unwrap(function.named_children[0])
    return function


def _no_query(expr: Expr) -> Expr:
    """``expr`` up to its query string or fragment (``${base}?${qs}`` → ``base``), its empty
    texts left out, one part as itself."""
    if isinstance(expr, Text):
        return Text(re.split(r"[?#]", expr.value, maxsplit=1)[0])
    if not isinstance(expr, Concat):
        return expr
    parts: list[Expr | None] = []
    for part in expr.parts:
        if isinstance(part, Text) and re.search(r"[?#]", part.value):
            parts.append(Text(re.split(r"[?#]", part.value, maxsplit=1)[0]))
            break
        parts.append(part)
    kept = tuple(p for p in parts if p != Text(""))
    if not kept:
        return Text("")
    return kept[0] if len(kept) == 1 and kept[0] is not None else Concat(kept)


def _unwrap_opt(node: Node | None) -> Node | None:
    return _unwrap(node) if node is not None else None


def _own(node: Node) -> Iterable[Node]:
    """The nodes under ``node``, not those of the functions in it."""
    stack = list(node.named_children)
    while stack:
        here = stack.pop()
        yield here
        if here.type not in _FUNCTIONS:
            stack.extend(here.named_children)


def _extended(body: Node, params: Sequence[str]) -> dict[str, tuple[int, Node, str]] | None:
    """A helper's local variables extended per key of an object parameter: ``name ->
    (parameter, what each key adds, the loop variable)`` (``for (const k of Object.keys(p))
    url += ...``). None: a local variable is assigned some other way (no helper)."""
    found: dict[str, tuple[int, Node, str]] = {}
    for node in _own(body):
        if node.type not in ("assignment_expression", "augmented_assignment_expression"):
            continue
        target = node.child_by_field_name("left")
        loop = next((a for a in ancestors(node) if a.type == "for_in_statement"), None)
        if (
            node.type != "augmented_assignment_expression"
            or _text(node.child_by_field_name("operator")) != "+="
            or target is None
            or target.type != "identifier"
            or loop is None
        ):
            return None
        right = _unwrap_opt(loop.child_by_field_name("right"))
        variable = loop.child_by_field_name("left")
        keys = right.child_by_field_name("function") if right is not None else None
        args = right.child_by_field_name("arguments") if right is not None else None
        over = args.named_children if args is not None else []
        if (
            right is None
            or right.type != "call_expression"
            or _text(keys) != "Object.keys"
            or len(over) != 1
            or _text(over[0]) not in params
            or variable is None
            or variable.type != "identifier"
        ):
            return None
        each = node.child_by_field_name("right")
        if each is None:
            return None
        found[_text(target)] = (list(params).index(_text(over[0])), each, _text(variable))
    return found


def _looks_like_url(text: str) -> bool:
    return text.startswith(("/", "http://", "https://", "{env:", "{import:", "{helper:", "//")) or (
        text.startswith(HOLE + "/")
    )


def env_values(project: Project, files: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """The values ``.env`` files give, by the folder they're in (``files``: path to text)."""
    found: dict[str, dict[str, str]] = {}
    for path, text in sorted(files.items()):
        folder, name = dirname(path), path.rsplit("/", 1)[-1]
        if name not in ENV_FILES:
            continue
        values = found.setdefault(folder, {})
        for raw in text.splitlines():
            matched = re.match(r"^\s*(?:export\s+)?([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", raw)
            if matched and matched[1] not in values:
                values[matched[1]] = matched[2].strip("'\"")
    del project
    return found


def env_for(path: str, envs: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    """The env values a file sees: its folder's and every folder's above it (nearest first)."""
    merged: dict[str, str] = {}
    folder = dirname(path)
    while True:
        for key, value in (envs.get(folder) or {}).items():
            merged.setdefault(key, value)
        if not folder:
            return merged
        folder = dirname(folder)


def scan_calls(
    result: ParseResult, definitions: Sequence[Definition], env: Mapping[str, str] | None = None
) -> FileCalls:
    """The HTTP calls (and URL constants, instances, wrappers) in one parsed file."""
    if result.language not in ECMA_LANGUAGES:
        return FileCalls(result.path)
    return _Scanner(result, definitions, env or {}).run()


# ---------------------------------------------------------------- joining the files


class _Joiner:
    def __init__(self, project: Project, files: Mapping[str, FileCalls]) -> None:
        self.project = project
        self.files = files
        configs = project.configs

        def read(path: str) -> bytes | None:
            text = configs.get(path)
            return text.encode() if text is not None else None

        # 3.3's resolution: tsconfig paths and baseUrl (extends followed), packages, index files.
        self.resolver = ModuleResolver(project.paths, ProjectConfig.load(project.paths, read))

    def module(self, path: str, source: str) -> str | None:
        """The file of the repository an import's source names."""
        found = self.resolver.resolve(path, source)
        return found.path if found.kind is ResolutionKind.FILE else None

    def imported(self, path: str, name: str, depth: int = 0) -> tuple[FileCalls, str] | None:
        """The file and name an imported ``name`` comes from (re-exports aren't followed)."""
        f = self.files.get(path)
        if f is None or depth > 3:
            return None
        source = f.imports.get(name)
        if source is None:
            return None
        target = self.module(path, source[0])
        other = self.files.get(target) if target else None
        if other is None:
            return None
        if source[1] == "default" and other.default is not None:
            return other, other.default  # the name its file exports as the default
        return other, source[1]

    def resolve(self, path: str, url: str | None, depth: int = 0) -> str | None:
        """``url`` with its ``{helper:N}`` parts evaluated (:mod:`~sensei_engine.link.
        url_helpers`) and its ``{import:X}`` parts filled in from the files they come from."""
        if url is None:
            return None
        if depth > MAX_RESOLVE_DEPTH:
            return _PLACEHOLDER_RE.sub(HOLE, url)
        if "{helper:" in url:
            f = self.files.get(path)

            def evaluate(index: int) -> str | None:
                if f is None or index >= len(f.helper_calls):
                    return None
                return url_helpers.call(self, path, f.helper_calls[index], depth)

            url = url_helpers.fill(url, evaluate)
        if "{import:" not in url:
            return url

        def fill(found: re.Match[str]) -> str:
            name = found[1]
            head, _, prop = name.partition(".")
            source = self.imported(path, head)
            if source is None:
                return HOLE
            other, original = source
            if prop:
                value = other.objects.get(original, {}).get(prop)
            else:
                value = other.constants.get(original)
            resolved = self.resolve(other.path, value, depth + 1)
            return resolved if resolved is not None else HOLE

        return re.sub(r"\{import:([^}]+)\}", fill, url)

    # what URL helpers read (url_helpers.Scope)

    def text(self, path: str, value: str, depth: int) -> str | None:
        return self.resolve(path, value, depth)

    def table(self, path: str, name: str) -> tuple[str, Mapping[str, str]] | None:
        f = self.files.get(path)
        if f is None:
            return None
        if name in f.objects:
            return path, f.objects[name]
        source = self.imported(path, name)
        if source is None or source[1] not in source[0].objects:
            return None
        return source[0].path, source[0].objects[source[1]]

    def helper(self, path: str, name: str) -> tuple[str, Helper] | None:
        f = self.files.get(path)
        if f is None:
            return None
        if name in f.helpers:
            return path, f.helpers[name]
        head, _, prop = name.partition(".")
        source = self.imported(path, head)
        if source is None:
            return None
        other, original = source
        wanted = prop if original == "*" and prop else original
        found = other.helpers.get(wanted)
        return (other.path, found) if found is not None else None

    def base(self, path: str, instance: str | None) -> str | None:
        """The baseURL of the axios instance ``instance`` names in ``path`` (None: not one)."""
        if instance is None:
            return None
        f = self.files[path]
        if instance in f.instances:
            return self.resolve(path, f.instances[instance]) or ""
        source = self.imported(path, instance)
        if source is not None and source[1] in source[0].instances:
            return self.resolve(source[0].path, source[0].instances[source[1]]) or ""
        return None

    def wrapper(self, path: str, name: str) -> tuple[FileCalls, Wrapper] | None:
        f = self.files[path]
        if name in f.wrappers:
            return f, f.wrappers[name]
        head, _, prop = name.partition(".")
        source = self.imported(path, head)
        if source is None:
            return None
        other, original = source
        wanted = prop if original == "*" and prop else original
        found = other.wrappers.get(wanted)
        return (other, found) if found is not None else None


@dataclass(slots=True)
class ClientCall:
    key: str
    path: str
    line: int
    caller: str
    method: str
    url_raw: str
    url_template: str | None
    base: str
    client: str
    via: tuple[str, ...] = ()  # the wrappers it went through
    operation: str | None = None

    @property
    def dynamic(self) -> bool:
        if self.url_template is None:
            return True
        literal = re.sub(r"\{[^}]*\}", "", self.url_template).strip("/")
        return not literal


@dataclass(slots=True)
class CallReport:
    calls: int = 0
    dynamic: int = 0
    through_wrappers: int = 0
    by_client: dict[str, int] = field(default_factory=dict[str, int])


@dataclass(slots=True)
class CallGraph:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    calls: list[HttpCall]
    report: CallReport


def _expand(joiner: _Joiner, path: str, site: CallSite) -> ClientCall | None:
    """A call site as a call: a wrapper call through its wrapper, the URL resolved."""
    via: tuple[str, ...] = ()
    url, method, client, instance, where = site.url, site.method, site.client, site.instance, path
    if site.wrapper is not None:
        found = joiner.wrapper(path, site.wrapper)
        if found is None:
            return None
        other, wrapper = found
        value = site.wrapper_args[wrapper.param] if wrapper.param < len(site.wrapper_args) else None
        filled = joiner.resolve(path, value)
        url = joiner.resolve(
            other.path, wrapper.template.replace("{param}", filled if filled is not None else HOLE)
        )
        method, client, instance, where = (
            wrapper.method,
            wrapper.client,
            wrapper.instance,
            other.path,
        )
        via = (wrapper.key,)
    else:
        url = joiner.resolve(path, url)
    base = joiner.base(where, instance)
    if base is not None:
        client = "axios"  # an instance from axios.create()
    template = url[:MAX_URL] if url is not None else None
    return ClientCall(
        key=f"http_client_call {method} {template or '?'} {path}:{site.line}",
        path=path,
        line=site.line,
        caller=site.caller,
        method=method,
        url_raw=site.url_raw,
        url_template=template,
        base=base or "",
        client=client,
        via=via,
        operation=site.operation,
    )


def _flatten_wrappers(files: Mapping[str, FileCalls], joiner: _Joiner) -> None:
    """Pending wrappers become wrappers once the wrapper they call is one, up to
    MAX_WRAPPER_DEPTH levels (a fixpoint)."""
    for _ in range(MAX_WRAPPER_DEPTH):
        changed = False
        for path, f in files.items():
            for pending in list(f.pending):
                inner = joiner.wrapper(path, pending.inner)
                if inner is None:
                    continue
                _, wrapper = inner
                arg = pending.args[wrapper.param] if wrapper.param < len(pending.args) else None
                found = _MARK_RE.search(arg or "")
                if arg is None or found is None:
                    f.pending.remove(pending)  # its URL parameter isn't one of ours
                    continue
                f.wrappers[pending.name] = Wrapper(
                    name=pending.name,
                    key=pending.key,
                    param=int(found[1]),
                    template=wrapper.template.replace("{param}", _MARK_RE.sub("{param}", arg)),
                    method=wrapper.method,
                    client=wrapper.client,
                    instance=wrapper.instance,
                )
                f.pending.remove(pending)
                changed = True
        if not changed:
            return


def client_calls(project: Project, files: Mapping[str, FileCalls]) -> CallGraph:
    """The repository's frontend HTTP calls as facts (see the module)."""
    kept = {
        path: f for path, f in files.items() if not f.server and not (f.empty and not f.pending)
    }
    joiner = _Joiner(project, kept)
    _flatten_wrappers(kept, joiner)
    report = CallReport()
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    calls: list[HttpCall] = []
    for path, f in sorted(kept.items()):
        for site in f.calls:
            call = _expand(joiner, path, site)
            if call is None:
                continue
            report.calls += 1
            report.by_client[call.client] = report.by_client.get(call.client, 0) + 1
            report.dynamic += call.dynamic
            report.through_wrappers += bool(call.via)
            nodes[call.key] = GraphNode(
                key=call.key,
                kind=NodeKind.HTTP_CLIENT_CALL,
                name=f"{call.method} {call.url_template or call.url_raw}"[:200],
                path=path,
                start_line=call.line,
                end_line=call.line,
                lang=project_lang(path),
                attrs={
                    "method": call.method,
                    "url_raw": call.url_raw,
                    "url_template": call.url_template,
                    "base_ref": call.base or None,
                    "dynamic": call.dynamic,
                    "client": call.client,
                    **({"via": list(call.via)} if call.via else {}),
                    **({"operation_id": call.operation} if call.operation else {}),
                },
                owner=path,
            )
            edges.append(
                GraphEdge(
                    call.caller,
                    call.key,
                    EdgeType.ISSUES_REQUEST,
                    Provenance.CONTRACT,
                    evidence_path=path,
                    evidence_line=call.line,
                    owner=path,
                )
            )
            calls.append(
                HttpCall(
                    key=call.key,
                    method=call.method,
                    url=call.url_template or "",
                    base=call.base,
                    dynamic=call.dynamic,
                    operation_id=call.operation,
                    evidence_path=path,
                    evidence_line=call.line,
                    owner=path,
                )
            )
    return CallGraph(list(nodes.values()), edges, calls, report)


def project_lang(path: str) -> str:
    suffix = path.rsplit(".", 1)[-1]
    return {"ts": "ts", "tsx": "tsx", "js": "js", "jsx": "jsx", "mjs": "js", "cjs": "js"}.get(
        suffix, "js"
    )


def scan_all(
    results: Iterable[tuple[ParseResult, Sequence[Definition]]],
    envs: Mapping[str, Mapping[str, str]],
) -> dict[str, FileCalls]:
    """:func:`scan_calls` over many files, each with the env values its folder sees."""
    return {
        result.path: scan_calls(result, definitions, env_for(result.path, envs))
        for result, definitions in results
    }
