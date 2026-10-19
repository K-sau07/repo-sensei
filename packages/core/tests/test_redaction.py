from __future__ import annotations

import pytest

from sensei_core.redaction import MASK, redact, redact_event, redact_text

GH_TOKEN = "ghs_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
FINE_GRAINED = "github_pat_" + "11ABCDEFG0123456789_abcdefghijKLMNOP"
ANTHROPIC = "sk-ant-" + "api03-abcdefghijklmnop"
VOYAGE = "pa-" + "abcdefghijklmnopqrstuvwxyz012345"
OUR_KEY = "rs_" + "AbCd1234_" + "ZyXwVuTsRqPoNmLk9876"
# Built from pieces so no key-like literal exists in the repository.
PEM_BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----"
PEM_END = "-----END RSA " + "PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"cloning with token {GH_TOKEN} now", GH_TOKEN),
        (f"pat={FINE_GRAINED}", FINE_GRAINED),
        (f"key {ANTHROPIC}", ANTHROPIC),
        (f"voyage {VOYAGE}", VOYAGE),
        (f"api key {OUR_KEY}", OUR_KEY),
        ("Authorization: Bearer abc.def.ghijklmnop", "abc.def.ghijklmnop"),
        (f"{PEM_BEGIN}\nMIIEpAIBAAKCAQEA\n{PEM_END}", "MIIEpAIBAAKCAQEA"),
    ],
)
def test_masks_known_credential_formats(text: str, secret: str) -> None:
    redacted = redact_text(text)
    assert secret not in redacted
    assert MASK in redacted


def test_masks_password_in_urls_but_keeps_user() -> None:
    url = "postgresql+asyncpg://sensei:supersecret@db:5432/sensei"
    redacted = redact_text(url)
    assert "supersecret" not in redacted
    assert redacted == f"postgresql+asyncpg://sensei:{MASK}@db:5432/sensei"


def test_leaves_ordinary_text_alone() -> None:
    text = "indexed 1,284 files in 42s for repo langflow (sha 5621dc)"
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "access_token",
        "client_secret",
        "password",
        "Authorization",
        "cookie",
        "api_key",
        "private_key",
        "X-CSRF-Token",
        "session_id",
    ],
)
def test_sensitive_keys_are_fully_masked(key: str) -> None:
    assert redact({key: "anything at all"}) == {key: MASK}


def test_nested_structures_are_walked() -> None:
    event = {
        "event": "github call",
        "headers": {"authorization": "token xyz", "accept": "json"},
        "args": [f"url?t={GH_TOKEN}", 3],
        "meta": ({"password": "p"},),
    }
    result = redact_event(event)
    assert result["headers"] == {"authorization": MASK, "accept": "json"}
    assert GH_TOKEN not in result["args"][0]
    assert result["args"][1] == 3
    assert result["meta"] == ({"password": MASK},)


def test_empty_sensitive_values_are_kept_visible() -> None:
    assert redact({"token": None}) == {"token": None}
    assert redact({"token": ""}) == {"token": ""}


def test_masks_password_in_userless_urls() -> None:
    assert redact_text("redis://:topsecret@localhost:6379/0") == f"redis://:{MASK}@localhost:6379/0"


@pytest.mark.parametrize(
    "line",
    [
        '"GET /api/v1/auth/github/callback?code=abc123&state=xyz789 HTTP/1.1" 302',
        "https://app.example.com/invite?token=secretinvite123",
        "https://s3.example.com/obj?X-Amz-Expires=60&signature=deadbeefcafe",
    ],
)
def test_masks_secrets_in_query_strings(line: str) -> None:
    redacted = redact_text(line)
    for secret in ("abc123", "xyz789", "secretinvite123", "deadbeefcafe"):
        assert secret not in redacted


def test_usage_counters_are_not_masked() -> None:
    usage = {"input_tokens": 120, "output_tokens": 40, "max_tokens": 1024, "tokens_out": 7}
    assert redact(usage) == usage


def test_very_deep_structures_are_still_redacted() -> None:
    deep: dict[str, object] = {"value": f"token {GH_TOKEN}"}
    for _ in range(20):
        deep = {"nested": deep}
    assert GH_TOKEN not in str(redact(deep))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '"GET /api/v1/invitations/Zx9_kq2L-7vYp0TzAbCdEfGh HTTP/1.1" 200',
            '"GET /api/v1/invitations/*** HTTP/1.1" 200',
        ),
        (
            "https://app.example.com/invite/Zx9_kq2L-7vYp0TzAbCdEfGh",
            "https://app.example.com/invite/***",
        ),
        ("/s/AbCdEfGhIjKlMnOpQr", "/s/***"),
        # Record ids are not secrets.
        ("/orgs/acme/invitations/0199f000", "/orgs/acme/invitations/0199f000"),
        (
            "DELETE /api/v1/orgs/acme/invitations/0199f000-0000-7000-8000-0000000000b1",
            "DELETE /api/v1/orgs/acme/invitations/0199f000-0000-7000-8000-0000000000b1",
        ),
    ],
)
def test_secret_path_segments_are_masked(text: str, expected: str) -> None:
    assert redact_text(text) == expected
