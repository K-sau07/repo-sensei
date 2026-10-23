"""Token counts for chunk budgets.

tiktoken's ``cl100k_base`` is a fast stand-in for the embedding model's tokenizer (2.7 calibrates
the budgets against Voyage's). Its ranks are downloaded on first use and cached in
``TIKTOKEN_CACHE_DIR`` (the worker image fetches them when it is built).
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache

import tiktoken

ENCODING = "cl100k_base"


@cache
def encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(ENCODING)


def count_tokens(text: str) -> int:
    return len(encoding().encode_ordinary(text))


def count_each(texts: Sequence[str]) -> list[int]:
    """The token count of each text (one call for many short texts, such as a file's lines)."""
    if not texts:
        return []
    return [len(tokens) for tokens in encoding().encode_ordinary_batch(list(texts))]
