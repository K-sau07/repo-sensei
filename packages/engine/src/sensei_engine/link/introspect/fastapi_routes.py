# pyright: basic
# (It reads whatever a repository's FastAPI version has, by duck typing: strict types don't fit.)
"""Runs in the sandbox (3.8), not in the engine: imports a repository's FastAPI apps and prints
their routes as one JSON line after :data:`MARKER`.

``python - <json>`` with this file on stdin; the argument is ``{"entries": ["app.main:app",
"pkg.factory:create_app()"]}``. The working directory is the repository's import root (on
``sys.path``). Only the standard library and the repository's own dependencies are used.

What it does for each entry:

1. imports the module (an import that fails on a missing setting is retried with a placeholder
   for each field pydantic says is missing, a few times);
2. takes the attribute, or calls it for ``name()`` (an app factory);
3. walks ``app.routes``: every ``APIRoute`` and ``APIWebSocketRoute`` with its full path (FastAPI
   has applied every ``include_router`` prefix by then), methods, name, the endpoint's module and
   qualified name, and tags; mounted sub-applications are walked too, under their mount path;
4. calls ``app.openapi()`` for the operation ids (a failure there is only noted).

The app is never started: no lifespan, no startup events, no server. Static-file mounts don't
check that their directory exists (a snapshot has no built frontend).
"""

import contextlib
import importlib
import inspect
import json
import os
import sys
import traceback
from typing import Any

MARKER = "SENSEI-ROUTES "
MAX_RETRIES = 5
PLACEHOLDERS = {
    "email": "user@example.com",
    "url": "http://localhost",
    "int": "1",
    "float": "1",
    "bool": "false",
}


def _no_dir_checks():
    """StaticFiles(check_dir=True) and its subclasses (FastAPI's frontend files) raise on a
    directory a snapshot doesn't have: never check."""
    try:
        from starlette.staticfiles import StaticFiles  # noqa: PLC0415 - may not be installed
    except ImportError:
        return
    with contextlib.suppress(ImportError):
        import fastapi.routing  # noqa: F401, PLC0415 - defines the subclasses to patch
    pending = [StaticFiles]
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        original = cls.__dict__.get("__init__")
        if original is None or getattr(original, "_sensei", False):
            continue
        try:
            accepts = "check_dir" in inspect.signature(original).parameters
        except (TypeError, ValueError):
            accepts = False
        if not accepts:
            continue

        def patched(self: object, *args: Any, _original: Any = original, **kwargs: Any) -> None:
            kwargs["check_dir"] = False
            _original(self, *args, **kwargs)

        patched._sensei = True  # type: ignore[attr-defined]
        cls.__init__ = patched


def _placeholder(error):
    kind = str(error.get("type", ""))
    for word, value in PLACEHOLDERS.items():
        if word in kind:
            return value
    return "placeholder"


def _missing_settings(exc):
    """Environment variables to set for a pydantic ValidationError about missing settings."""
    errors = getattr(exc, "errors", None)
    if errors is None:
        return {}
    found = {}
    try:
        listed = errors()
    except Exception:  # anything but pydantic's: nothing to fix
        return {}
    for error in listed:
        loc = error.get("loc") or ()
        if not loc:
            continue
        name = str(loc[0])
        value = _placeholder(error)
        for variant in (name, name.upper()):
            if os.environ.get(variant) != value:
                found[variant] = value
    return found


def _forget(root):
    """Drop the repository's modules, so an import is tried afresh."""
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None) or ""
        if path.startswith(root):
            del sys.modules[name]


def _load(entry, root, notes):
    module_name, _, attr = entry.partition(":")
    call = attr.endswith("()")
    attr = attr.removesuffix("()")
    for _ in range(MAX_RETRIES):
        try:
            module = importlib.import_module(module_name)
            target = getattr(module, attr)
            return target() if call else target
        except Exception as exc:  # a repository's import can raise anything
            missing = _missing_settings(exc)
            if not missing:
                notes.append(f"{entry}: {type(exc).__name__}: {str(exc)[:500]}")
                notes.append(traceback.format_exc(limit=-3)[-2000:])
                return None
            os.environ.update(missing)
            notes.append(f"{entry}: placeholder settings {sorted(missing)}")
            _forget(root)
    notes.append(f"{entry}: still missing settings after {MAX_RETRIES} tries")
    return None


def _emit(route, path, kind, endpoint, out, *, context=None):
    methods = sorted(getattr(route, "methods", None) or ())
    if "WebSocket" in kind:
        methods = ["WS"]
    tags = (getattr(context, "tags", None) if context is not None else None) or getattr(
        route, "tags", None
    )
    out.append(
        {
            "path": path,
            "methods": methods,
            "name": getattr(route, "name", None),
            "module": getattr(endpoint, "__module__", None),
            "qualname": getattr(endpoint, "__qualname__", None),
            "tags": [str(tag) for tag in tags or ()],
            "api": kind in ("APIRoute", "APIWebSocketRoute"),
            "include_in_schema": bool(getattr(route, "include_in_schema", True)),
        }
    )


def _included(router, prefix, seen, out):
    """FastAPI 0.13x+ keeps an included router as one route that works out its routes when
    asked (``effective_candidates``): each is a route in context, or another included router."""
    candidates = list(router.effective_candidates())
    low: Any = getattr(router, "effective_low_priority_routes", None)
    extra: list[Any] = list(low()) if callable(low) else []  # pyright: ignore[reportArgumentType]
    candidates += [c for c in extra if c not in candidates]
    for candidate in candidates:
        if hasattr(candidate, "effective_candidates"):
            _included(candidate, prefix, seen, out)
            continue
        original = getattr(candidate, "original_route", None)
        endpoint = getattr(candidate, "endpoint", None) or getattr(original, "endpoint", None)
        if original is None or endpoint is None:
            continue
        path = prefix + str(getattr(candidate, "path", "") or getattr(original, "path", ""))
        _emit(original, path, type(original).__name__, endpoint, out, context=candidate)


def _routes(app, prefix, seen, out):
    if id(app) in seen:
        return
    seen.add(id(app))
    for route in getattr(app, "routes", None) or ():
        if hasattr(route, "effective_candidates"):
            _included(route, prefix, seen, out)
            continue
        path = prefix + str(getattr(route, "path", ""))
        endpoint = getattr(route, "endpoint", None)
        kind = type(route).__name__
        if kind == "Mount" or (endpoint is None and hasattr(route, "app")):
            sub = getattr(route, "app", None)
            if sub is not None and hasattr(sub, "routes"):
                _routes(sub, path.rstrip("/"), seen, out)
            continue
        if endpoint is not None:
            _emit(route, path, kind, endpoint, out)


def _operation_ids(app, notes):
    try:
        schema = app.openapi()
    except Exception as exc:  # whatever the app raises
        notes.append(f"openapi(): {type(exc).__name__}: {str(exc)[:300]}")
        return {}
    ids = {}
    for path, item in (schema.get("paths") or {}).items():
        for method, operation in (item or {}).items():
            if isinstance(operation, dict) and operation.get("operationId"):
                ids[f"{method.upper()} {path}"] = operation["operationId"]
    return ids


def main():
    request = json.loads(sys.argv[1])
    root = os.getcwd()
    sys.path.insert(0, root)
    _no_dir_checks()
    apps = []
    notes = []
    for entry in request.get("entries", []):
        app = _load(entry, root, notes)
        if app is None or not hasattr(app, "routes"):
            if app is not None:
                notes.append(f"{entry}: not an app ({type(app).__name__})")
            continue
        routes = []
        _routes(app, "", set(), routes)
        apps.append(
            {
                "entry": entry,
                "routes": routes,
                "operation_ids": _operation_ids(app, notes) if hasattr(app, "openapi") else {},
                "root_path": str(getattr(app, "root_path", "") or ""),
            }
        )
    sys.stdout.write(MARKER + json.dumps({"apps": apps, "notes": notes}) + "\n")


if __name__ == "__main__":
    main()
