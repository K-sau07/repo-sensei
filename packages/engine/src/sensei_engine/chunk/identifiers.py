"""Splitting identifiers into words, for keyword search over code.

``split_identifier("getUserByID")`` is ``"getUserByID get user by id"``: the whole token first (an
exact match still finds it), then its words, lowercased. Words come from camelCase and PascalCase
humps, acronyms (``HTTPServer``: ``http server``), digits that end a word (``base64Encode``:
``base64 encode``) and the separators of snake_case, kebab-case, SCREAMING_CASE and dotted paths
(``os.path.join``). A token that is a single word comes back as it is.
"""

from __future__ import annotations

import re

_LOWER = r"[^\W\d_A-Z]"  # a letter that isn't an ASCII capital (other scripts count as lower)
_SEPARATORS = re.compile(r"[\W_]+")
_WORDS = re.compile(
    rf"[A-Z]+\d*(?=[A-Z]{_LOWER})"  # an acronym before a word: HTTP|Server, HTTP2|Client
    rf"|[A-Z]?{_LOWER}+(?:\d+{_LOWER}*)*"  # a word with the digits in or after it: get, User, i18n
    r"|[A-Z]+\d*"  # an acronym at the end: ID, HTTP2
    rf"|\d+{_LOWER}*"  # digits first: 404, 2fa
)
_IDENTIFIER = re.compile(r"[^\W\d][\w$]*")


def words(token: str) -> list[str]:
    """The words of ``token``, lowercased."""
    return [word.lower() for piece in _SEPARATORS.split(token) for word in _WORDS.findall(piece)]


def split_identifier(token: str) -> str:
    """``token`` followed by its words, lowercased (``getUserByID get user by id``)."""
    found = words(token)
    if not found or found == [token.lower()]:
        return token
    return " ".join([token, *found])


def identifier_terms(text: str) -> str:
    """The words of every compound identifier in ``text`` (``user_id``: ``user id``), once per
    identifier, in order of first appearance. The identifiers themselves aren't repeated."""
    seen: set[str] = set()
    terms: list[str] = []
    for token in _IDENTIFIER.findall(text):
        if token in seen:
            continue
        seen.add(token)
        found = words(token)
        if len(found) > 1 or (found and found[0] != token.lower()):
            terms.extend(found)
    return " ".join(terms)
