"""URL helpers (3.9): functions that return a URL, such as ``getURL("API_KEY")`` or
``getBaseUrl()``, summarized where they are defined and evaluated where a call uses them.

A helper's return value is kept as an expression over its parameters (:data:`Expr`):

- text: a URL template, whose ``{import:X}`` parts are filled in when the files are joined;
- a parameter;
- concatenation;
- ``a ?? b`` and ``a || b``;
- ``flag ? a : b`` on a parameter (its default when the call leaves it out);
- a constant object indexed by a parameter (``URLs[key]``);
- a call to another helper;
- a local variable extended once for each key of an object parameter
  (``for (const k of Object.keys(params)) url += `/${params[k]}```).

A call in a URL is evaluated with its arguments: strings, booleans, object literals and
``undefined``. A part that can't be known is a ``{}`` hole. A helper whose value depends on an
argument that can't be known (an object that isn't a literal, a key that isn't a string) has no
value (None), so the call's URL is unknown rather than wrong.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

HOLE = "{}"
MAX_DEPTH = 6  # helpers calling helpers


@dataclass(frozen=True, slots=True)
class Text:
    value: str


@dataclass(frozen=True, slots=True)
class Param:
    index: int


@dataclass(frozen=True, slots=True)
class Flag:
    value: bool


@dataclass(frozen=True, slots=True)
class Undefined:
    pass


@dataclass(frozen=True, slots=True)
class Concat:
    parts: tuple[Expr | None, ...]


@dataclass(frozen=True, slots=True)
class Either:
    """``first ?? second`` / ``first || second``: the first known and not empty."""

    first: Expr | None
    second: Expr | None


@dataclass(frozen=True, slots=True)
class Choice:
    """``param ? then : otherwise``."""

    param: int
    then: Expr | None
    otherwise: Expr | None


@dataclass(frozen=True, slots=True)
class Lookup:
    """A constant object (of the helper's file, or imported into it) indexed by a parameter."""

    table: str
    param: int


@dataclass(frozen=True, slots=True)
class Call:
    name: str  # as written: ``getBaseUrl``, ``urls.base``
    args: tuple[Expr | None, ...]


@dataclass(frozen=True, slots=True)
class ObjectLiteral:
    items: tuple[tuple[str, Expr | None], ...]


@dataclass(frozen=True, slots=True)
class PerKey:
    """``base``, then ``each`` once for every value of the object parameter ``param``."""

    base: Expr | None
    param: int
    each: Expr | None


@dataclass(frozen=True, slots=True)
class KeyValue:
    """Inside :class:`PerKey`'s ``each``: the value of the key it is at."""


type Expr = (
    Text | Param | Flag | Undefined | Concat | Either | Choice | Lookup | Call | ObjectLiteral
    | PerKey | KeyValue
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Helper:
    """A function returning a URL (see the module)."""

    name: str
    body: Expr
    defaults: tuple[Expr | None, ...]  # each parameter's default (None: none, or not known)


@dataclass(frozen=True, slots=True)
class HelperCall:
    """A call in a URL, kept until the files are joined (its helper may be imported)."""

    name: str
    args: tuple[Expr | None, ...]


class Scope(Protocol):
    """What evaluating needs from the joined files."""

    def text(self, path: str, value: str, depth: int) -> str | None:
        """``value`` (of ``path``) with its ``{import:X}`` parts filled in."""
        ...

    def table(self, path: str, name: str) -> tuple[str, Mapping[str, str]] | None:
        """The constant object ``name`` means in ``path``: its file and properties."""
        ...

    def helper(self, path: str, name: str) -> tuple[str, Helper] | None:
        """The helper ``name`` means in ``path``: its file and summary."""
        ...


@dataclass(frozen=True, slots=True)
class _Object:
    values: tuple[Value, ...]


class _Absent:
    """An argument the call leaves out (or passes as ``undefined``)."""


ABSENT = _Absent()
type Value = str | bool | _Object | _Absent | None


class _Unknown(Exception):
    """The helper's value depends on what can't be known."""


def call(scope: Scope, path: str, site: HelperCall, depth: int = 0) -> str | None:
    """The URL a call in ``path`` to a helper gives (None: not a helper, or not knowable)."""
    try:
        args = tuple(_value(scope, path, arg, (), depth) for arg in site.args)
        return _call(scope, path, site.name, args, depth)
    except _Unknown:
        return None


def _call(scope: Scope, path: str, name: str, args: Sequence[Value], depth: int) -> str | None:
    found = scope.helper(path, name) if depth <= MAX_DEPTH else None
    if found is None:
        return None
    where, helper = found
    bound: list[Value] = []
    for index, default in enumerate(helper.defaults):
        given = args[index] if index < len(args) else ABSENT
        if given is ABSENT and default is not None:
            given = _value(scope, where, default, (), depth + 1)
        bound.append(given)
    result = _value(scope, where, helper.body, bound, depth + 1)
    return result if isinstance(result, str) else None


def _string(value: Value) -> str:
    return value if isinstance(value, str) else HOLE


def _truth(value: Value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, _Absent):
        return False
    if isinstance(value, _Object):
        return True
    if isinstance(value, str) and "{" not in value:
        return bool(value)
    return None


def _value(  # noqa: PLR0911, PLR0912 - one case per kind of expression
    scope: Scope,
    path: str,
    expr: Expr | None,
    bound: Sequence[Value],
    depth: int,
    *,
    current: Value = None,
) -> Value:
    def again(inner: Expr | None) -> Value:
        return _value(scope, path, inner, bound, depth, current=current)

    match expr:
        case None:
            return None
        case Text(value):
            return scope.text(path, value, depth)
        case Param(index):
            return bound[index] if index < len(bound) else ABSENT
        case Flag(value):
            return value
        case Undefined():
            return ABSENT
        case KeyValue():
            return current
        case ObjectLiteral(items):
            return _Object(tuple(again(v) for _, v in items))
        case Concat(parts):
            return "".join(_string(again(p)) for p in parts)
        case Either(first, second):
            value = again(first)
            if isinstance(value, str) and value:
                return value
            return again(second)
        case Choice(param, then, otherwise):
            truth = _truth(bound[param] if param < len(bound) else ABSENT)
            if truth is None:
                a = again(then)
                b = again(otherwise)
                return a if a == b else None
            return again(then if truth else otherwise)
        case Lookup(table, param):
            key = bound[param] if param < len(bound) else ABSENT
            found = scope.table(path, table)
            if not isinstance(key, str) or "{" in key or found is None:
                raise _Unknown
            where, values = found
            if key not in values:
                raise _Unknown
            return scope.text(where, values[key], depth)
        case Call(name, args):
            given = tuple(again(a) for a in args)
            return _call(scope, path, name, given, depth + 1)
        case PerKey(base, param, each):
            text = _string(again(base))
            values = bound[param] if param < len(bound) else ABSENT
            if isinstance(values, _Absent):
                return text
            if not isinstance(values, _Object):
                raise _Unknown  # how many keys, and which, isn't known
            for value in values.values:
                text += _string(_value(scope, path, each, bound, depth, current=value))
            return text


def urlish(expr: Expr | None) -> bool:
    """Whether a function returning ``expr`` builds a URL: a path, a full URL, an environment
    value or a constant (imported) in it, or a constant object looked up."""
    match expr:
        case Text(value):
            return "/" in value or value.startswith(("{import:", "{env:", "{helper:"))
        case Lookup():
            return True
        case Concat(parts):
            return any(urlish(p) for p in parts)
        case Either(first, second) | Choice(_, first, second):
            return urlish(first) or urlish(second)
        case PerKey(base, _, each):
            return urlish(base) or urlish(each)
        case _:
            return False


_PLACEHOLDER_RE = re.compile(r"\{helper:(\d+)\}")


def fill(url: str, value: Callable[[int], str | None]) -> str:
    """``url`` with each ``{helper:N}`` replaced by ``value(N)`` (a hole when None)."""

    def one(found: re.Match[str]) -> str:
        result = value(int(found[1]))
        return result if result is not None else HOLE

    return _PLACEHOLDER_RE.sub(one, url)
