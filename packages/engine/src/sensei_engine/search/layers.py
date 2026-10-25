"""The layer of a search hit (2.9.3): which part of the application it is, for the swimlanes.

From the file's role (:mod:`sensei_engine.ingest.roles`), the hit's kind and its path; the
router's :class:`~sensei_engine.search.route.Layer` values, folded into lanes by
:data:`~sensei_engine.search.route.LANES`:

* ``test``, ``docs``, ``config`` and ``db`` files are those layers;
* in an ``api`` file, a router module (``router.py``, ``routes.ts``, ``urls.py``) is ``route``
  when the hit isn't a function; everything else there is ``handler``;
* in a ``ui`` file, a generated or hand-written API client (a ``client``, ``api``, ``sdk``,
  ``services`` or ``controllers`` directory, ``*.gen.ts``) is ``client``; hooks, stores and
  contexts (``use…`` files, ``stores/``, ``contexts/``) are ``state``; the rest is ``ui``;
* ``service`` code is ``worker`` under ``workers``, ``tasks``, ``jobs`` or ``celery``, else
  ``service``;
* anything else has no layer (the ``other`` lane).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from sensei_engine.search.route import LANES, Lane, Layer

_FIXED = {"test": Layer.TEST, "docs": Layer.DOCS, "config": Layer.CONFIG, "db": Layer.DB}
_ROUTER_STEMS = frozenset({"router", "routers", "routes", "urls", "routing"})
_CALLABLE_KINDS = frozenset({"function", "method", "component", "hook", "openapi_operation"})
_CLIENT_DIRS = frozenset({"client", "clients", "api", "apis", "sdk", "services", "controllers"})
_STATE_DIRS = frozenset({"store", "stores", "state", "redux", "contexts", "context", "hooks"})
_WORKER_DIRS = frozenset({"worker", "workers", "tasks", "jobs", "celery", "queues"})
_GENERATED_CLIENT = re.compile(r"\.gen\.[jt]sx?$")
_HOOK_FILE = re.compile(r"^use[A-Z\-_]")


def _api(kind: str, file: PurePosixPath) -> Layer:
    stem = file.name.split(".", 1)[0].lower()
    return Layer.ROUTE if stem in _ROUTER_STEMS and kind not in _CALLABLE_KINDS else Layer.HANDLER


def _ui(kind: str, file: PurePosixPath) -> Layer:
    directories = {part.lower() for part in file.parts[:-1]}
    if directories & _CLIENT_DIRS or _GENERATED_CLIENT.search(file.name):
        return Layer.CLIENT
    stem = file.name.split(".", 1)[0]
    if kind == "hook" or directories & _STATE_DIRS or _HOOK_FILE.match(stem):
        return Layer.STATE
    return Layer.UI


def _service(_: str, file: PurePosixPath) -> Layer:
    directories = {part.lower() for part in file.parts[:-1]}
    return Layer.WORKER if directories & _WORKER_DIRS else Layer.SERVICE


_BY_ROLE = {"api": _api, "ui": _ui, "service": _service}


def layer_of(role: str | None, kind: str, path: str) -> Layer | None:
    """The layer of a hit of ``kind`` in a file of ``role`` at ``path``; see the module."""
    if role in _FIXED:
        return _FIXED[role]
    tag = _BY_ROLE.get(role or "")
    return tag(kind, PurePosixPath(path)) if tag is not None else None


def lane_of(layer: Layer | None) -> Lane:
    return LANES[layer] if layer is not None else Lane.OTHER
