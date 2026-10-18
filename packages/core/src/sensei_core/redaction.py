"""Secret redaction for logs, error reports and anything else that leaves the process.

Two layers:
  * keys that look sensitive (``token``, ``password``, ``authorization`` ...) have their whole
    value replaced;
  * string values are scanned for well-known credential formats, which are masked in place.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

MASK = "***"

SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|cookie|api[_-]?key|private[_-]?key|"
    r"credential|session[_-]?id|csrf)",
    re.IGNORECASE,
)

_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub tokens: classic/oauth/user/server/refresh and fine-grained PATs.
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # Anthropic and Voyage API keys.
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bpa-[A-Za-z0-9_\-]{20,}"),
    # Our own API keys (rs_<prefix>_<secret>).
    re.compile(r"\brs_[A-Za-z0-9]{8}_[A-Za-z0-9]{16,}\b"),
    # Bearer tokens in headers or messages.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{10,}=*"),
    # PEM private key blocks.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)

# user:password@ inside URLs: keep the user, mask the password.
_URL_CREDENTIALS_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)(?P<user>[^:/@\s]+):[^@/\s]+@")


def redact_text(text: str) -> str:
    """Mask credential-looking substrings inside free text."""
    redacted = _URL_CREDENTIALS_RE.sub(r"\g<scheme>\g<user>:" + MASK + "@", text)
    for pattern in _VALUE_PATTERNS:
        redacted = pattern.sub(MASK, redacted)
    return redacted


def redact(value: Any, *, key: str | None = None, _depth: int = 0) -> Any:
    """Return a redacted copy of ``value`` (dicts, lists, tuples and strings are walked)."""
    if key is not None and SENSITIVE_KEY_RE.search(key) and value not in (None, ""):
        return MASK
    if _depth > 8:
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {k: redact(v, key=str(k), _depth=_depth + 1) for k, v in mapping.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        sequence = cast("Sequence[object]", value)
        items = [redact(item, _depth=_depth + 1) for item in sequence]
        return tuple(items) if isinstance(value, tuple) else items
    return value


def redact_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Redact a structured log event dict (top-level keys checked by name)."""
    return {k: redact(v, key=k) for k, v in event.items()}
