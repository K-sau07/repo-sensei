"""What the client does with a response (3.22): ``ClientOutcome`` nodes, for JavaScript and
TypeScript, so a trace can come back up from the data to what the caller of a request does with
its answer (it stores a token, goes to another page, sets its state, shows an error).

:func:`scan_outcomes` reads one parsed file, :func:`link_outcomes` makes the facts.

**Outcomes:**

- *storage*: ``localStorage``/``sessionStorage`` ``.setItem``/``.removeItem``/``.clear`` (the key
  when it is a literal, or a constant of the file), ``document.cookie = …``, ``Cookies.set(…)``;
- *navigate*: ``navigate("/x")`` (react-router's ``useNavigate``, with ``{replace}``, and
  TanStack Router's ``navigate({to})``), ``router.push/replace("/x")`` (Next's ``useRouter``),
  ``history.push``, ``redirect("/x")``, ``window.location.href = "/x"``,
  ``window.location.assign/replace``, and a ``<Navigate to="/x" />`` element; the target path
  when it is a literal or a template;
- *state*: a ``useState`` setter (``setOrders(res.data)``), a setter or a store's function taken
  from a hook (``const { setUser } = useAuth()``, ``useAuthStore((s) => s.login)``), a context's
  function (``const { login } = useContext(AuthContext)``), a store's
  ``setState``/``getState().f()``, ``dispatch(action(…))``, and react-query's
  ``queryClient.setQueryData/invalidateQueries/…`` (the query key as the target);
- *error*: what an error branch shows (``toast.error``, ``showErrorToast``, ``handleError``,
  ``alert``), and ``onError: handleError`` itself.

**Only what follows a response.** An outcome is kept when it is *tied* to a call whose answer it
handles, in the function around it: the latest ``await`` before it (``const res = await
api.login(…)``; then ``res``, ``res.data`` or a destructured name may flow into it); a ``.then``
/ ``.catch`` callback of a call (its parameters are the result); a ``catch`` of a ``try`` that
awaits; react-query's ``onSuccess``/``onError``/``onSettled`` beside a ``mutationFn``/``queryFn``
(tied to that function, a local one by its lines; ``mutate(x, {onSuccess})`` to the hook
``mutate`` came from); or, in a component, a value a query hook gives
(``const { data } = useQuery(…)``) used by it or by the condition around it. An anonymous
callback (``setTimeout(() => navigate("/"))``, ``forEach``) is part of the function around it; a
named function, or a JSX handler, is not. Anything else (a button's ``navigate``, a setter run on
input) is not a response's outcome and is left out. What an error branch does is marked
``on="error"``.

Facts: a ``ClientOutcome`` node per outcome (``client_outcome <kind> <target> <path>:<line>
after <a>-<b>[,<c>-<d>...]``, the last part the lines it is tied to: the call, then the hook its
function came from or the function a mutation runs, which a trace reads, :func:`outcome_tie`;
attrs ``kind``, ``action``, ``target``, ``value``, ``tie``, ``awaits`` (the call as written),
``after_call`` (the ``HttpClientCall`` whose result flows into it, when the awaited call is one),
``on``) and ``PRODUCES`` from the innermost definition around it, on its line: ``contract``
for the browser's own objects, ``receiver_heuristic`` for a name bound from a known hook or
import, ``name_unique`` for one known by its name only (an unbound ``navigate``, a ``setX``
from the props).
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from tree_sitter import Node

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.project import Definition
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.link.http_calls import ECMA_LANGUAGES, project_lang
from sensei_engine.link.pytree import ancestors, walk
from sensei_engine.parse.parser import ParseResult

OUTCOME_KINDS = ("storage", "navigate", "state", "error")
MAX_TEXT = 120

_FUNCTIONS = frozenset({
    "function_declaration", "function_expression", "arrow_function", "method_definition",
    "function", "generator_function_declaration",
})  # fmt: skip
_STORAGES = frozenset({
    "localStorage", "sessionStorage", "window.localStorage", "window.sessionStorage",
})  # fmt: skip
_STORAGE_METHODS = frozenset({"setItem", "removeItem", "clear"})
_COOKIE_OBJECTS = frozenset({"Cookies", "Cookie", "cookies", "cookies()", "cookieStore"})
_COOKIE_METHODS = frozenset({"set", "remove", "delete"})
_LOCATIONS = frozenset({"window.location", "location", "document.location"})
_LOCATION_TARGETS = frozenset({
    "window.location", "window.location.href", "location.href", "document.location",
    "document.location.href", "window.location.pathname",
})  # fmt: skip
_QUERY_METHODS = frozenset({
    "setQueryData", "setQueriesData", "invalidateQueries", "resetQueries", "refetchQueries",
    "removeQueries", "clear",
})  # fmt: skip
_NAVIGATE_HOOKS = frozenset({"useNavigate"})
_ROUTER_HOOKS = frozenset({"useRouter"})
_HISTORY_HOOKS = frozenset({"useHistory"})
_DISPATCH_HOOKS = frozenset({"useDispatch", "useAppDispatch"})
_STATE_HOOKS = frozenset({"useState", "useReducer", "React.useState", "React.useReducer"})
_QUERY_CLIENT_HOOKS = frozenset({"useQueryClient"})
_CONTEXT_HOOKS = frozenset({"useContext", "React.useContext"})
# Hooks whose values are a response (a component's ``data`` from them is what came back).
_QUERY_HOOKS = frozenset({
    "useQuery", "useSuspenseQuery", "useInfiniteQuery", "useSWR", "useSWRInfinite",
    "useSWRImmutable",
})  # fmt: skip
_TOASTS = frozenset({"toast", "message", "notification", "notify", "enqueueSnackbar"})
_REDIRECTS = frozenset({"redirect", "permanentRedirect"})
_RESULT_CALLBACKS = frozenset({"onSuccess", "onError", "onSettled"})
_ERROR_NAMES = re.compile(r"toast|notif|alert|snackbar|showError|handleError|onError", re.I)
_STORE = re.compile(r"^use\w*Store$")
_STORE_OBJECT = re.compile(r"^(?:use)?\w*Store$")
_SETTER = re.compile(r"^set[A-Z]\w*$")
_NOT_SETTERS = frozenset({"setTimeout", "setInterval", "setImmediate"})
_HOOK = re.compile(r"^use[A-Z]\w*$")
_TIE = re.compile(r" after (\d+-\d+(?:,\d+-\d+)*)$")
_KEY_KIND = re.compile(r"^client_outcome (\w+) ")


Spans = tuple[tuple[int, int], ...]  # (first line, last line)s


def outcome_key(
    kind: str, target: str, path: str, line: int, ties: Spans, *, action: str | None = None
) -> str:
    """An outcome's node key: its kind, target and place, and the lines it is tied to (a trace
    reads them back, :func:`outcome_tie`); ``action`` when another outcome of the same kind and
    target is on its line (``localStorage.setItem('t', …); sessionStorage.setItem('t', …)``)."""
    after = ",".join(f"{start}-{end}" for start, end in ties)
    place = f"{path}:{line}" if action is None else f"{path}:{line} {action}"
    return f"client_outcome {kind} {target or '?'} {place} after {after}"


def outcome_tie(key: str) -> Spans | None:
    """The lines an outcome's key says it is tied to (None: not an outcome's): the call whose
    response it follows first, then what that call comes from (the hook that gave ``mutate``,
    the function a mutation runs)."""
    found = _TIE.search(key)
    if found is None or not key.startswith("client_outcome "):
        return None
    spans = (part.split("-") for part in found[1].split(","))
    return tuple((int(start), int(end)) for start, end in spans)


def outcome_kind(key: str) -> str | None:
    """storage, navigate, state or error, as an outcome's key says (None: not an outcome's)."""
    found = _KEY_KIND.match(key)
    return found[1] if found and found[1] in OUTCOME_KINDS else None


@dataclass(frozen=True, slots=True)
class OutcomeSite:
    line: int
    scope: str  # the innermost definition's key (the module's at top level)
    kind: str  # storage | navigate | state | error
    action: str  # what does it, as written: localStorage.setItem, navigate, setOrders, ...
    target: str  # the key, the path, the setter ("": not known)
    value: str  # the value it is given, short
    ties: Spans  # the lines of the call whose response it follows, then what it comes from
    provenance: Provenance
    awaits: str | None = None  # that call, as written
    after: int | None = None  # that call's line, when its result flows into the outcome
    on_error: bool = False


@dataclass(slots=True)
class FileOutcomes:
    path: str
    sites: list[OutcomeSite] = field(default_factory=list[OutcomeSite])


@dataclass(frozen=True, slots=True)
class _Tie:
    spans: Spans
    awaits: str | None
    results: frozenset[str]  # the names its result is bound to
    on_error: bool


def _text(node: Node | None) -> str:
    raw = node.text if node is not None else None
    return raw.decode("utf-8", "replace") if raw else ""


def _short(node: Node | None) -> str:
    return re.sub(r"\s+", " ", _text(node)).strip()[:MAX_TEXT]


def _unwrap(node: Node) -> Node:
    """Through parentheses, ``as``, ``!`` and ``await``."""
    while (
        node.type
        in (
            "parenthesized_expression",
            "as_expression",
            "non_null_expression",
            "satisfies_expression",
            "type_assertion",
            "await_expression",
        )
        and node.named_children
    ):
        node = node.named_children[0]
    return node


def _string(node: Node) -> str | None:
    if node.type != "string":
        return None
    return "".join(_text(c) for c in node.named_children if c.type == "string_fragment")


def _callee(call: Node) -> Node | None:
    function = call.child_by_field_name("function")
    return _unwrap(function) if function is not None else None


def _args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    if args is None or args.type != "arguments":
        return []
    return [a for a in args.named_children if a.type != "comment"]


def _object(node: Node) -> dict[str, Node]:
    """An object literal's plain properties (``{to: x, "replace": y}``)."""
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


def _own(node: Node) -> Iterator[Node]:
    """The nodes under ``node`` in source order, not those of the functions in it."""
    stack = list(reversed(node.named_children))
    while stack:
        here = stack.pop()
        yield here
        if here.type not in _FUNCTIONS:
            stack.extend(reversed(here.named_children))


def _function_of(node: Node) -> Node | None:
    return next((a for a in ancestors(node) if a.type in _FUNCTIONS), None)


def _names(pattern: Node | None) -> set[str]:
    """The names a binding pattern binds (``res``, ``{ data: user, x }``, ``[a, b]``)."""
    if pattern is None:
        return set()
    if pattern.type in ("identifier", "shorthand_property_identifier_pattern"):
        return {_text(pattern)}
    found: set[str] = set()
    for child in pattern.named_children:
        if child.type == "pair_pattern":
            found |= _names(child.child_by_field_name("value"))
        elif child.type in ("object_assignment_pattern", "assignment_pattern"):
            found |= _names(child.child_by_field_name("left"))
        elif child.type in (
            "identifier",
            "shorthand_property_identifier_pattern",
            "object_pattern",
            "array_pattern",
            "rest_pattern",
        ):
            found |= _names(child)
    return found


def _params(function: Node) -> set[str]:
    single = function.child_by_field_name("parameter")
    if single is not None:
        return _names(single)
    params = function.child_by_field_name("parameters")
    found: set[str] = set()
    for param in params.named_children if params is not None else ():
        pattern = param.child_by_field_name("pattern") if param.type != "identifier" else param
        found |= _names(pattern if pattern is not None else param)
    return found


def _identifiers(nodes: Iterable[Node | None]) -> set[str]:
    found: set[str] = set()
    for node in nodes:
        if node is None:
            continue
        for inner in walk(node):
            if inner.type in ("identifier", "shorthand_property_identifier"):
                found.add(_text(inner))
    return found


def _span(node: Node) -> tuple[int, int]:
    return node.start_point.row + 1, node.end_point.row + 1


def _awaited_call(node: Node) -> Node | None:
    """The call an ``await`` waits for (in TypeScript, ``await f<T>(x)`` puts it in the call)."""
    inner = _unwrap(node.named_children[0]) if node.named_children else None
    if inner is not None and inner.type == "call_expression":
        return inner
    parent = node.parent
    if parent is not None and parent.type == "call_expression":
        return parent
    return None


def _bound_to(node: Node) -> set[str]:
    """The names the value at ``node`` is bound to (``const {data} = <node>``, ``res = <node>``)."""
    parent = node.parent
    while parent is not None and parent.type in ("await_expression", "parenthesized_expression"):
        node, parent = parent, parent.parent
    if parent is None:
        return set()
    if parent.type == "variable_declarator" and parent.child_by_field_name("value") == node:
        return _names(parent.child_by_field_name("name"))
    if parent.type == "assignment_expression" and parent.child_by_field_name("right") == node:
        return _names(parent.child_by_field_name("left"))
    return set()


_CHAIN = frozenset({"then", "catch", "finally"})


class _Scanner:
    def __init__(self, result: ParseResult, definitions: Sequence[Definition]) -> None:
        self.result = result
        self.out = FileOutcomes(result.path)
        module = next((d for d in definitions if d.kind == "module"), None)
        self.module_key = module.symbol_key if module else f"{result.language} {result.path}/"
        self._scopes = sorted(
            (d.start_line, d.end_line, d.symbol_key)
            for d in definitions
            if d.kind in ("function", "method", "component", "hook")
        )
        self.imports: dict[str, str] = {}  # local name -> the module it is imported from
        # Each function body's awaited calls, and where each ends (see :meth:`awaits`).
        self._awaits: dict[int, tuple[list[Node], list[int]]] = {}
        self._declared: dict[int, dict[str, tuple[int, Node]]] = {}  # see :meth:`declared`
        self._bound: dict[int, frozenset[str]] = {}  # see :meth:`bound`
        self.constants: dict[str, str] = {}  # the module's string constants
        self.navigators: set[str] = set()
        self.routers: set[str] = set()
        self.histories: set[str] = set()
        self.dispatchers: set[str] = set()
        self.setters: set[str] = set()  # useState's, and those a hook or a store gives
        self.query_clients: set[str] = set()
        # A query hook's values, by the function they're bound in: name -> (the hook call).
        self.queries: dict[tuple[int, str], Node] = {}

    def caller(self, line: int) -> str:
        found = self.module_key
        for start, end, key in self._scopes:
            if start > line:
                break
            if line <= end:
                found = key
        return found

    def run(self) -> FileOutcomes:
        tree = self.result.tree
        if tree is None or not self.result.ok:
            return self.out
        root = tree.root_node
        for node in walk(root):
            if node.type == "import_statement":
                self.import_statement(node)
            elif node.type == "variable_declarator":
                self.declarator(node)
        seen: set[tuple[int, str, str]] = set()
        for node in walk(root):
            found = self.outcome(node)
            if found is None:
                continue
            kind, action, target, value, provenance, forced_error = found
            tie = self.pair_tie(node) if node.type == "pair" else self.tie(node, value)
            if tie is None:
                continue
            spans, awaits, used, on_error = tie
            line = node.start_point.row + 1
            if (line, action, target) in seen:
                continue
            seen.add((line, action, target))
            self.out.sites.append(
                OutcomeSite(
                    line=line,
                    scope=self.caller(line),
                    kind=kind,
                    action=action[:MAX_TEXT],
                    target=target[:MAX_TEXT],
                    value=_short(value) if value is not None else "",
                    ties=spans,
                    provenance=provenance,
                    awaits=awaits,
                    after=spans[0][0] if used else None,
                    on_error=on_error or forced_error,
                )
            )
        return self.out

    # what the file binds

    def import_statement(self, node: Node) -> None:
        source = node.child_by_field_name("source")
        module = _string(source) if source is not None else None
        if not module:
            return
        for spec in walk(node):
            if spec.type == "import_specifier":
                alias = spec.child_by_field_name("alias") or spec.child_by_field_name("name")
                self.imports[_text(alias)] = module
            elif spec.type == "import_clause":
                for part in spec.named_children:
                    if part.type == "identifier":
                        self.imports[_text(part)] = module

    def declarator(self, node: Node) -> None:  # noqa: PLR0912 - one case per kind of hook
        name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
        if name is None or value is None:
            return
        value = _unwrap(value)
        if value.type == "string" and node.parent is not None and node.parent.parent is not None:
            if node.parent.parent.type in ("program", "export_statement"):
                text = _string(value)
                if text is not None and name.type == "identifier":
                    self.constants[_text(name)] = text
            return
        if value.type != "call_expression":
            return
        callee = _text(_callee(value))
        names = _names(name)
        if callee in _NAVIGATE_HOOKS:
            self.navigators |= names
        elif callee in _ROUTER_HOOKS:
            self.routers |= names
        elif callee in _HISTORY_HOOKS:
            self.histories |= names
        elif callee in _DISPATCH_HOOKS:
            self.dispatchers |= names
        elif callee in _QUERY_CLIENT_HOOKS:
            self.query_clients |= names
        elif callee in _STATE_HOOKS:
            # const [x, setX] = useState(): the second is the setter (useReducer's dispatch).
            elements = [c for c in name.named_children if c.type != "comment"]
            if name.type == "array_pattern" and len(elements) >= 2:
                self.setters |= _names(elements[1])
        elif _STORE.match(callee) or callee in _CONTEXT_HOOKS:
            # A store's functions (useAuthStore((s) => s.login)), a context's (const { login }
            # = useContext(AuthContext)).
            self.setters |= names
        elif callee.split(".")[-1] in _QUERY_HOOKS:
            function = _function_of(node)
            at = function.start_byte if function is not None else -1
            for each in names:
                self.queries[(at, each)] = value
        elif _HOOK.match(callee.split(".")[-1]) and name.type == "object_pattern":
            self.setters |= {n for n in names if _SETTER.match(n)}

    def known(self, name: str) -> bool:
        """``name`` is imported from a router's, Next's or a toast's package."""
        return name in self.imports

    # what an outcome is

    def target(self, node: Node | None) -> str:  # noqa: PLR0911 - one case per kind of value
        """A path or a key as written: a literal, a template, a constant of the module, the
        ``to`` of an object (``navigate({to: "/"})``), ``back`` for ``navigate(-1)``."""
        if node is None:
            return ""
        node = _unwrap(node)
        if node.type == "string":
            return _string(node) or ""
        if node.type == "template_string":
            return _text(node)[1:-1]
        if node.type == "identifier":
            return self.constants.get(_text(node), "")
        if node.type == "object":
            props = _object(node)
            for key in ("to", "pathname", "href", "url", "queryKey"):
                if key in props:
                    return self.target(props[key]) or _short(props[key])
            return ""
        if node.type == "unary_expression" and _text(node).startswith("-"):
            return "back"
        if node.type == "array":
            return _short(node)
        return ""

    def outcome(  # noqa: PLR0911, PLR0912 - one case per kind of outcome
        self, node: Node
    ) -> tuple[str, str, str, Node | None, Provenance, bool] | None:
        """``(kind, action, target, value, provenance, an error's)`` when ``node`` is an
        outcome (see the module)."""
        if node.type == "assignment_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            written = _text(left)
            if written == "document.cookie" and right is not None:
                cookie = self.target(right).split("=", 1)[0].strip()
                return "storage", "document.cookie", cookie, right, Provenance.CONTRACT, False
            if written in _LOCATION_TARGETS:
                return "navigate", written, self.target(right), right, Provenance.CONTRACT, False
            return None
        if node.type in ("jsx_self_closing_element", "jsx_opening_element"):
            name = node.child_by_field_name("name")
            if _text(name) != "Navigate":
                return None
            for attribute in node.named_children:
                parts = attribute.named_children
                if attribute.type == "jsx_attribute" and len(parts) > 1 and _text(parts[0]) == "to":
                    value = parts[-1]
                    if value.type == "jsx_expression" and value.named_children:
                        value = value.named_children[0]
                    sure = self.provenance(self.known("Navigate"))
                    return "navigate", "<Navigate>", self.target(value), value, sure, False
            return None
        if node.type == "pair":
            return self.on_error_value(node)
        if node.type != "call_expression":
            return None
        callee = _callee(node)
        if callee is None:
            return None
        args = _args(node)
        first = args[0] if args else None
        if callee.type == "member_expression":
            return self.member_outcome(callee, args)
        if callee.type != "identifier":
            return None
        name = _text(callee)
        if name in self.navigators or name == "navigate":
            sure = self.provenance(name in self.navigators)
            return "navigate", "navigate", self.target(first), first, sure, False
        if name in _REDIRECTS:
            sure = self.provenance(self.known(name))
            return "navigate", name, self.target(first), first, sure, False
        if name in self.setters:
            value = first
            return "state", name, name, value, Provenance.RECEIVER_HEURISTIC, False
        if name in self.dispatchers or name == "dispatch":
            action = (
                _callee(_unwrap(first))
                if first is not None and _unwrap(first).type == ("call_expression")
                else None
            )
            target = _text(action) if action is not None else _short(first)
            sure = self.provenance(name in self.dispatchers)
            return "state", "dispatch", target, first, sure, False
        if _SETTER.match(name) and name not in _NOT_SETTERS:
            return "state", name, name, first, Provenance.NAME_UNIQUE, False
        if _ERROR_NAMES.search(name) and self.in_error(node):
            sure = self.provenance(self.known(name))
            return "error", name, name, first, sure, False
        return None

    def member_outcome(  # noqa: PLR0911, PLR0912 - one case per kind of outcome
        self, callee: Node, args: Sequence[Node]
    ) -> tuple[str, str, str, Node | None, Provenance, bool] | None:
        receiver_node = callee.child_by_field_name("object")
        receiver = _text(receiver_node)
        method = _text(callee.child_by_field_name("property"))
        first = args[0] if args else None
        action = f"{receiver}.{method}"
        if receiver in _STORAGES and method in _STORAGE_METHODS:
            value = args[1] if len(args) > 1 else None
            return "storage", action, self.target(first), value, Provenance.CONTRACT, False
        if receiver in _COOKIE_OBJECTS and method in _COOKIE_METHODS:
            value = args[1] if len(args) > 1 else None
            sure = self.provenance(self.known(receiver))
            return "storage", action, self.target(first), value, sure, False
        if receiver in _LOCATIONS and method in ("assign", "replace"):
            return "navigate", action, self.target(first), first, Provenance.CONTRACT, False
        if method in ("push", "replace"):
            if receiver in self.routers or receiver in self.histories:
                return "navigate", action, self.target(first), first, self.provenance(True), False
            if receiver in ("router", "Router", "history"):
                return "navigate", action, self.target(first), first, Provenance.NAME_UNIQUE, False
        if method in _QUERY_METHODS:
            bound = receiver in self.query_clients
            if bound or re.search(r"queryClient$", receiver, re.I):
                sure = self.provenance(bound)
                return "state", f"queryClient.{method}", self.target(first), first, sure, False
        if method == "setState" and _STORE_OBJECT.match(receiver):
            return "state", action, receiver, first, Provenance.RECEIVER_HEURISTIC, False
        store = re.fullmatch(r"(use\w*Store)\.getState\(\)", receiver)
        if store is not None:
            return "state", action, method, first, Provenance.RECEIVER_HEURISTIC, False
        if receiver in _TOASTS and method in ("error", "warn", "warning"):
            sure = self.provenance(self.known(receiver))
            return "error", action, action, first, sure, True
        if re.match(r"console\b", receiver):
            return None
        if self.in_error(callee) and (_ERROR_NAMES.search(receiver) or _ERROR_NAMES.search(method)):
            sure = self.provenance(self.known(receiver.split(".")[0]))
            return "error", action, action, first, sure, False
        return None

    def on_error_value(
        self, pair: Node
    ) -> tuple[str, str, str, Node | None, Provenance, bool] | None:
        """``onError: handleError`` (or ``handleError.bind(showErrorToast)``) beside a
        ``mutationFn``: the function an error goes to."""
        key, value = pair.child_by_field_name("key"), pair.child_by_field_name("value")
        if _text(key) != "onError" or value is None or value.type in _FUNCTIONS:
            return None
        handler = value
        if value.type == "call_expression":
            callee = _callee(value)
            if callee is None or not _text(callee).endswith(".bind"):
                return None
            handler = callee.child_by_field_name("object") or callee
        if handler.type not in ("identifier", "member_expression"):
            return None
        name = _text(handler)
        sure = self.provenance(self.known(name.split(".")[0]))
        bound = _args(value) if value.type == "call_expression" else []
        return "error", name, name, bound[0] if bound else None, sure, True

    @staticmethod
    def provenance(bound: bool) -> Provenance:
        return Provenance.RECEIVER_HEURISTIC if bound else Provenance.NAME_UNIQUE

    @staticmethod
    def in_error(node: Node) -> bool:
        """Whether ``node`` is in an error branch: a ``catch``, a ``.catch`` callback, an
        ``onError``, ``.then``'s second callback."""
        for around in ancestors(node):
            if around.type == "catch_clause":
                return True
            if around.type in _FUNCTIONS:
                parent = around.parent
                if (
                    parent is not None
                    and parent.type == "pair"
                    and _text(parent.child_by_field_name("key")) == "onError"
                ):
                    return True
                if parent is not None and parent.type == "arguments":
                    call = parent.parent
                    callee = _callee(call) if call is not None else None
                    if callee is not None and callee.type == "member_expression":
                        method = _text(callee.child_by_field_name("property"))
                        index = [c for c in parent.named_children if c.type != "comment"]
                        if method == "catch" or (
                            method == "then" and len(index) > 1 and index[1] == around
                        ):
                            return True
        return False

    # what an outcome follows

    def tie(self, node: Node, value: Node | None) -> tuple[Spans, str | None, bool, bool] | None:
        """The call ``node`` handles the response of (see the module): its lines (and what it
        comes from), the call as written, whether its result flows into ``node``, whether it's
        an error's branch."""
        used = _identifiers(
            [value, *(c for c in node.named_children if c.type == "arguments")]
            if node.type == "call_expression"
            else [value]
        )
        found = self.tied(node, used)
        if found is None:
            return self.query_tie(node, used)
        return found.spans, found.awaits, bool(found.results & used), found.on_error

    def pair_tie(self, pair: Node) -> tuple[Spans, str | None, bool, bool] | None:
        """``onError: handleError`` is tied as an ``onError`` callback is."""
        found = self.callback_tie(pair.parent, True, None) if pair.parent is not None else None
        return (found.spans, found.awaits, False, True) if found is not None else None

    def tied(self, node: Node, used: set[str]) -> _Tie | None:  # noqa: PLR0911, PLR0912
        """The call ``node`` follows, walking out through anonymous callbacks (see the
        module): an await whose result it uses, else the latest before it."""
        here = node
        while True:
            function = _function_of(here)
            if function is None:
                return None
            body = function.child_by_field_name("body")
            # In a catch of a try that awaits: an error of what it awaits.
            for around in ancestors(here):
                if around == function:
                    break
                if around.type == "catch_clause" and around.parent is not None:
                    block = around.parent.child_by_field_name("body")
                    calls = self.awaits(block) if block is not None else []
                    if calls:
                        span = (_span(calls[0])[0], _span(calls[-1])[1])
                        hooks = [h for c in calls for h in self.via_hook(c)]
                        awaits = _short(_callee(calls[0]))
                        return _Tie((span, *hooks), awaits, frozenset(), True)
            # An await before it in the same function: one whose result it uses, else the latest.
            if body is not None:
                before = self.awaits_before(body, here.start_byte)
                if before:
                    # The latest whose result it uses, else the latest: from the end, so an
                    # outcome right after its await costs one look, not one per await before.
                    call = next((c for c in reversed(before) if self.bound(c) & used), before[-1])
                    results = self.bound(call)
                    spans = (_span(call), *self.via_hook(call))
                    return _Tie(spans, _short(_callee(call)), results, False)
            parent = function.parent
            # A callback of a promise: .then(r => …), .catch(e => …).
            if parent is not None and parent.type == "arguments" and parent.parent is not None:
                call = parent.parent
                callee = _callee(call)
                if callee is not None and callee.type == "member_expression":
                    method = _text(callee.child_by_field_name("property"))
                    if method in _CHAIN:
                        root = self.chain_root(call)
                        args = [c for c in parent.named_children if c.type != "comment"]
                        error = method == "catch" or (
                            method == "then" and len(args) > 1 and args[1] == function
                        )
                        results = frozenset[str]() if error else frozenset(_params(function))
                        spans = (_span(root), *self.via_hook(root))
                        return _Tie(spans, _short(_callee(root)), results, error)
            # react-query's onSuccess/onError/onSettled, beside the function it runs.
            if parent is not None and parent.type == "pair":
                key = _text(parent.child_by_field_name("key"))
                if key in _RESULT_CALLBACKS and parent.parent is not None:
                    return self.callback_tie(parent.parent, key == "onError", function)
                if key in _RESULT_CALLBACKS:
                    return None
            if self.named(function):
                return None
            here = function

    def query_tie(self, node: Node, used: set[str]) -> tuple[Spans, str | None, bool, bool] | None:
        """A value a query hook gave the component (``const { data } = useQuery(…)``), used by
        ``node`` or by the condition it is under."""
        function = next(
            (a for a in ancestors(node) if a.type in _FUNCTIONS and self.named(a)), None
        )
        if function is None or not self.queries:
            return None
        names = set(used)
        for around in ancestors(node):
            if around == function:
                break
            if around.type in ("if_statement", "ternary_expression"):
                names |= _identifiers([around.child_by_field_name("condition")])
            elif around.type == "binary_expression" and _text(
                around.child_by_field_name("operator")
            ) in ("&&", "||", "??"):
                names |= _identifiers([around.child_by_field_name("left")])
        # The hook's values bound in this function, or in one it is nested in.
        scopes = [a.start_byte for a in ancestors(node) if a.type in _FUNCTIONS]
        for at in scopes:
            for name in sorted(names):
                hook = self.queries.get((at, name))
                if hook is not None:
                    flows = name in used
                    return (_span(hook),), _short(_callee(hook)), flows, False
        return None

    def callback_tie(self, options: Node, error: bool, function: Node | None) -> _Tie | None:
        """``useMutation({mutationFn, onSuccess})``: tied to the function it runs (a local one by
        its lines), else to the call the options are given to (``mutate(x, {onSuccess})``, with
        the hook ``mutate`` came from, and a function handed to it beside them:
        ``mutate(key, fn, {onSuccess})``); None for options of nothing (a constant, a ``new
        QueryCache({onError})``'s)."""
        props = _object(options)
        run = props.get("mutationFn") or props.get("queryFn")
        results = (
            frozenset(_params(function)) if function is not None and not error else frozenset[str]()
        )
        if run is not None:
            target = _unwrap(run)
            if target.type == "identifier":
                local = self.local_function(target, _text(target))
                return _Tie((_span(local or target),), _text(target), results, error)
            body = target.child_by_field_name("body") if target.type in _FUNCTIONS else None
            body = _unwrap(body) if body is not None else None
            awaits = (
                _short(_callee(body))
                if body is not None and body.type == "call_expression"
                else None
            )
            return _Tie((_span(target),), awaits, results, error)
        around = options.parent
        call = around.parent if around is not None and around.type == "arguments" else None
        if call is None or call.type != "call_expression":
            return None
        runs: list[tuple[int, int]] = []
        for arg in map(_unwrap, _args(call)):
            if arg.type in _FUNCTIONS:
                runs.append(_span(arg))
            elif arg.type == "identifier" and (local := self.local_function(arg, _text(arg))):
                runs.append(_span(local))
        spans = (_span(call), *self.via_hook(call), *runs)
        return _Tie(spans, _short(_callee(call)), results, error)

    def via_hook(self, call: Node) -> list[tuple[int, int]]:
        """The hook the function ``call`` calls came from, by its lines (``const { mutate } =
        useLoginUser()``, then ``mutate(…)`` or ``login.mutateAsync(…)``): a trace reaches the
        hook from that line."""
        callee = _callee(call)
        while callee is not None and callee.type == "member_expression":
            inner = callee.child_by_field_name("object")
            callee = _unwrap(inner) if inner is not None else None
        if callee is None or callee.type != "identifier":
            return []
        name = _text(callee)
        for block in ancestors(call):
            if block.type not in ("statement_block", "program"):
                continue
            declared = self.declared(block).get(name)
            if declared is None or declared[0] >= call.start_byte:
                continue  # not declared in this block before the call: the one around it
            value = declared[1].child_by_field_name("value")
            value = _unwrap(value) if value is not None else None
            hook = _text(_callee(value)).split(".")[-1] if value is not None else ""
            if value is not None and value.type == "call_expression" and _HOOK.match(hook):
                return [_span(value)]
            return []
        return []

    def declared(self, block: Node) -> dict[str, tuple[int, Node]]:
        """Each name a block's declarations bind, first: (its statement's start, the
        declarator); once per block, as every call in it asks."""
        held = self._declared.get(block.id)
        if held is not None:
            return held
        found: dict[str, tuple[int, Node]] = {}
        for statement in block.named_children:
            if statement.type not in ("lexical_declaration", "variable_declaration"):
                continue
            for declarator in statement.named_children:
                if declarator.type != "variable_declarator":
                    continue
                for bound in _names(declarator.child_by_field_name("name")):
                    found.setdefault(bound, (statement.start_byte, declarator))
        self._declared[block.id] = found
        return found

    def awaits(self, body: Node) -> list[Node]:
        """The calls ``body`` awaits, in order (not those of the functions in it, nor an
        outcome awaited: ``await queryClient.invalidateQueries()``); once per body, as every
        outcome in it asks."""
        held = self._awaits.get(body.id)
        if held is not None:
            return held[0]
        found: list[Node] = []
        for node in _own(body):
            if node.type == "await_expression":
                call = _awaited_call(node)
                if call is not None and self.outcome(call) is None:
                    found.append(call)
        self._awaits[body.id] = (found, [call.end_byte for call in found])
        return found

    def bound(self, call: Node) -> frozenset[str]:
        """:func:`_bound_to`, once per call."""
        held = self._bound.get(call.id)
        if held is None:
            held = self._bound[call.id] = frozenset(_bound_to(call))
        return held

    def awaits_before(self, body: Node, offset: int) -> list[Node]:
        """The calls ``body`` awaits that end by ``offset``, in order."""
        found = self.awaits(body)
        return found[: bisect.bisect_right(self._awaits[body.id][1], offset)]

    @staticmethod
    def chain_root(call: Node) -> Node:
        """The call a ``.then().catch()`` chain starts from."""
        while True:
            callee = _callee(call)
            if callee is None or callee.type != "member_expression":
                return call
            if _text(callee.child_by_field_name("property")) not in _CHAIN:
                return call
            inner = callee.child_by_field_name("object")
            inner = _unwrap(inner) if inner is not None else None
            if inner is None or inner.type != "call_expression":
                return call
            call = inner

    @staticmethod
    def named(function: Node) -> bool:
        """A function the definitions have: declared, a method, or a variable's or a
        property's value (not a callback handed to a call, nor a JSX handler)."""
        if function.type in ("function_declaration", "method_definition"):
            return True
        parent = function.parent
        return parent is not None and parent.type in ("variable_declarator", "pair")

    @staticmethod
    def local_function(node: Node, name: str) -> Node | None:
        """The function a ``const``/``function`` named ``name`` in a block around ``node`` is."""
        for block in ancestors(node):
            if block.type not in ("statement_block", "program"):
                continue
            for statement in block.named_children:
                if (
                    statement.type == "function_declaration"
                    and _text(statement.child_by_field_name("name")) == name
                ):
                    return statement
                if statement.type in ("lexical_declaration", "variable_declaration"):
                    for declarator in statement.named_children:
                        if (
                            declarator.type == "variable_declarator"
                            and _text(declarator.child_by_field_name("name")) == name
                        ):
                            value = declarator.child_by_field_name("value")
                            if value is not None and _unwrap(value).type in _FUNCTIONS:
                                return _unwrap(value)
        return None


def scan_outcomes(result: ParseResult, definitions: Sequence[Definition]) -> FileOutcomes:
    """The response outcomes in one parsed file (see the module)."""
    if result.language not in ECMA_LANGUAGES:
        return FileOutcomes(result.path)
    return _Scanner(result, definitions).run()


# ---------------------------------------------------------------- facts


@dataclass(slots=True)
class OutcomeReport:
    outcomes: int = 0
    after_call: int = 0  # whose result flows from an HTTP call's
    on_error: int = 0
    by_kind: dict[str, int] = field(default_factory=dict[str, int])


@dataclass(slots=True)
class OutcomeGraph:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    report: OutcomeReport


def link_outcomes(files: Mapping[str, FileOutcomes], calls: Iterable[GraphNode]) -> OutcomeGraph:
    """The outcomes as facts (see the module); ``calls``: the HTTP calls' nodes (an outcome a
    call's result flows into names it)."""
    at: dict[tuple[str, int], str] = {}
    for call in sorted(calls, key=lambda n: n.key):
        if call.kind is NodeKind.HTTP_CLIENT_CALL and call.path is not None:
            at.setdefault((call.path, call.start_line), call.key)
    report = OutcomeReport()
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for path, found in sorted(files.items()):
        for site in found.sites:
            key = outcome_key(site.kind, site.target, path, site.line, site.ties)
            held = nodes.get(key)
            if held is not None and held.attrs.get("action") != site.action:
                # Another outcome of the kind and target on the line: its own node.
                key = outcome_key(
                    site.kind, site.target, path, site.line, site.ties, action=site.action
                )
                held = nodes.get(key)
            if held is not None:
                continue
            after_call = at.get((path, site.after)) if site.after is not None else None
            report.outcomes += 1
            report.by_kind[site.kind] = report.by_kind.get(site.kind, 0) + 1
            report.after_call += after_call is not None
            report.on_error += site.on_error
            shown = (
                site.action if site.target in ("", site.action) else f"{site.action} {site.target}"
            )
            nodes[key] = GraphNode(
                key=key,
                kind=NodeKind.CLIENT_OUTCOME,
                name=shown[:200],
                path=path,
                start_line=site.line,
                end_line=site.line,
                lang=project_lang(path),
                attrs={
                    "kind": site.kind,
                    "action": site.action,
                    "target": site.target or None,
                    "value": site.value or None,
                    "tie": [list(span) for span in site.ties],
                    **({"awaits": site.awaits} if site.awaits else {}),
                    **({"after_call": after_call} if after_call else {}),
                    **({"on": "error"} if site.on_error else {}),
                },
                owner=path,
            )
            edges.append(
                GraphEdge(
                    site.scope,
                    key,
                    EdgeType.PRODUCES,
                    site.provenance,
                    evidence_path=path,
                    evidence_line=site.line,
                    owner=path,
                )
            )
    return OutcomeGraph(list(nodes.values()), edges, report)
