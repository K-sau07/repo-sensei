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

# user:password@ inside URLs: keep the user (possibly empty, e.g. redis://:pw@host), mask the
# password.
_URL_CREDENTIALS_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)(?P<user>[^:/@\s]*):[^@/\s]+@")

# Secrets carried in query strings: OAuth codes/state, tokens, signatures, keys.
_QUERY_SECRET_RE = re.compile(
    r"(?P<key>[?&](?:code|state|token|access_token|refresh_token|id_token|key|api_key|"
    r"signature|sig|secret|password|invite|share)=)[^&\s#\"']+",
    re.IGNORECASE,
)

# Bearer links carry their secret in the path: /invite/<token>, /invitations/<token>, /s/<token>.
# UUIDs (e.g. /orgs/acme/invitations/<id>) are record ids, not secrets, and stay readable.
_PATH_SECRET_RE = re.compile(
    r"(?P<prefix>/(?:invite|invitations|s)/)"
    r"(?![0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b)"
    r"[A-Za-z0-9_\-]{16,}"
)

# Usage counters such as input_tokens / max_tokens / tokens_out are numbers, not credentials.
_COUNTER_KEY_RE = re.compile(r"tokens|token_count|token_usage", re.IGNORECASE)


def redact_text(text: str) -> str:
    """Mask credential-looking substrings inside free text."""
    redacted = _URL_CREDENTIALS_RE.sub(r"\g<scheme>\g<user>:" + MASK + "@", text)
    redacted = _QUERY_SECRET_RE.sub(r"\g<key>" + MASK, redacted)
    redacted = _PATH_SECRET_RE.sub(r"\g<prefix>" + MASK, redacted)
    for pattern in _VALUE_PATTERNS:
        redacted = pattern.sub(MASK, redacted)
    return redacted


def is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key)) and not _COUNTER_KEY_RE.search(key)


def redact(value: Any, *, key: str | None = None, _depth: int = 0) -> Any:
    """Return a redacted copy of ``value`` (dicts, lists, tuples and strings are walked)."""
    if key is not None and is_sensitive_key(key) and value not in (None, ""):
        return MASK
    if _depth > 12:
        # Too deep to walk safely: fall back to text redaction of the whole value.
        return redact_text(str(value))
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
