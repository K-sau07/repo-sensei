"""Splitting identifiers into words, for keyword search over code.

``split_identifier("getUserByID")`` is ``"getUserByID get user by id"``: the whole token first (an
exact match still finds it), then its words, lowercased. Words come from camelCase and PascalCase
humps, acronyms (``HTTPServer``: ``http server``), digits that end a word (``base64Encode``:
``base64 encode``) and the separators of snake_case, kebab-case, SCREAMING_CASE and dotted paths
(``os.path.join``). A token that is a single word comes back as it is.

:func:`index_terms` turns a text into the terms keyword search indexes and queries.
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
_TERM = re.compile(r"[^\W\d][\w$]*|\d+")
MAX_TERM = 100


def words(token: str) -> list[str]:
    """The words of ``token``, lowercased."""
    return [word.lower() for piece in _SEPARATORS.split(token) for word in _WORDS.findall(piece)]


def split_identifier(token: str) -> str:
    """``token`` followed by its words, lowercased (``getUserByID get user by id``)."""
    found = words(token)
    if not found or found == [token.lower()]:
        return token
    return " ".join([token, *found])


def index_terms(text: str) -> str:
    """``text`` as keyword-search terms: each identifier (or number) as one lowercase term,
    its words joined (``read_items``: ``readitems``, ``getUserById``: ``getuserbyid``), then,
    for a compound one, its words; punctuation goes.

    This is what the BM25 indexes see. Postgres's parser would split ``read_items`` into its words
    (losing the identifier) and keep ``self.session.get`` or ``app/models.py`` whole (losing
    ``session`` and ``models``); terms made here pass through it unchanged. Tokens over
    :data:`MAX_TERM` characters (encoded data) are left out."""
    terms: list[str] = []
    for token in _TERM.findall(text):
        if len(token) > MAX_TERM:
            continue
        found = words(token)
        if not found:
            continue
        terms.append("".join(found))
        if len(found) > 1:
            terms.extend(found)
    return " ".join(terms)
