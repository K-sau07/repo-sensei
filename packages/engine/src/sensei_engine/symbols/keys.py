"""Symbol keys: stable, SCIP-like names for definitions that don't change when lines move.

A key is ``<lang> <relpath>/<descriptors>``:

- ``lang`` is ``py``, ``ts``, ``tsx``, ``js``, ``jsx`` or ``java`` (from the file's language);
- the descriptors name the definition and its containers, outermost first:
  ``Name#`` for a type (class, interface, type alias, enum, namespace), ``name().`` for a
  callable (function, method, component, hook), ``name.`` for a value (variable, field);
- the module itself has no descriptor (``py backend/app/models.py/``);
- a name that isn't a plain identifier is quoted in backticks (a backtick inside is doubled);
- a key already taken in the file (overloads, a property's setter, redefinitions) gets
  ``~<n>`` after its descriptor, by source order: the second ``f`` is ``f().~1``. Java's
  overloaded methods and constructors are numbered so too, not keyed by their parameter types:
  a method's key then stays put when its parameters change, and a method that isn't overloaded
  keeps the plain key. An overload added after the others leaves their keys alone; one added
  before them renumbers those after it.

Examples: ``py backend/app/api/routes/items.py/read_items().``,
``py backend/app/models.py/Item#``, ``py backend/app/core/config.py/Settings#emails_enabled().``,
``tsx frontend/src/components/Items/AddItem.tsx/AddItem().onSubmit().``,
``java src/main/java/com/acme/shop/web/OrderController.java/OrderController#getOrders().``.
"""

from __future__ import annotations

import enum
import re

KEY_LANGUAGES: dict[str, str] = {
    "python": "py",
    "typescript": "ts",
    "tsx": "tsx",
    "javascript": "js",
    "jsx": "jsx",
    "java": "java",
}

_PLAIN = re.compile(r"[\w$]+")


class Descriptor(enum.StrEnum):
    TYPE = "#"
    CALLABLE = "()."
    VALUE = "."


def escape(name: str) -> str:
    if _PLAIN.fullmatch(name):
        return name
    return "`" + name.replace("`", "``") + "`"


def descriptor(name: str, suffix: Descriptor) -> str:
    return f"{escape(name)}{suffix.value}"


def module_key(language: str, path: str) -> str:
    return f"{KEY_LANGUAGES[language]} {path}/"


class KeyAllocator:
    """Hands out a file's keys, numbering repeats (``~1``, ``~2``) in the order asked."""

    def __init__(self, language: str, path: str) -> None:
        self.module = module_key(language, path)
        self._seen: dict[str, int] = {}

    def allocate(self, container: str, own: str) -> str:
        """The key for descriptor ``own`` inside ``container`` (a key from this allocator, or
        :attr:`module`); a repeat gets the next ``~<n>``."""
        key = container + own
        count = self._seen.get(key, 0)
        self._seen[key] = count + 1
        return key if count == 0 else f"{key}~{count}"
