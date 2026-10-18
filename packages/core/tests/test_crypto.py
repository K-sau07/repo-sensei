from __future__ import annotations

import base64
import os

import pytest
from pydantic import SecretStr

from sensei_core.config import DEV_TOKEN_ENCRYPTION_KEY, AuthSettings, Settings
from sensei_core.security.crypto import DecryptionError, TokenCipher


def _key() -> bytes:
    return os.urandom(32)


def test_round_trip() -> None:
    cipher = TokenCipher(_key())
    blob = cipher.encrypt_text("gho_example_token", context="user-1")
    assert b"gho_example_token" not in blob
    assert cipher.decrypt_text(blob, context="user-1") == "gho_example_token"


def test_same_plaintext_encrypts_differently() -> None:
    cipher = TokenCipher(_key())
    assert cipher.encrypt(b"same") != cipher.encrypt(b"same")


def test_tampering_is_detected() -> None:
    cipher = TokenCipher(_key())
    blob = bytearray(cipher.encrypt(b"secret"))
    blob[-1] ^= 0x01
    with pytest.raises(DecryptionError, match="authentication"):
        cipher.decrypt(bytes(blob))


def test_context_is_bound() -> None:
    cipher = TokenCipher(_key())
    blob = cipher.encrypt(b"secret", context=b"user-1")
    with pytest.raises(DecryptionError):
        cipher.decrypt(blob, context=b"user-2")


def test_unknown_key_is_rejected() -> None:
    blob = TokenCipher(_key()).encrypt(b"secret")
    with pytest.raises(DecryptionError, match="unknown key"):
        TokenCipher(_key()).decrypt(blob)


def test_rotation_reads_old_ciphertext_and_writes_with_new_key() -> None:
    old, new = _key(), _key()
    legacy = TokenCipher(old).encrypt(b"secret")
    rotated = TokenCipher(new, previous=old)
    assert rotated.decrypt(legacy) == b"secret"
    assert TokenCipher(new).decrypt(rotated.encrypt(b"fresh")) == b"fresh"


@pytest.mark.parametrize("blob", [b"", b"v1", b"v2" + b"x" * 40, b"v1" + b"x" * 10])
def test_malformed_input_is_rejected(blob: bytes) -> None:
    with pytest.raises(DecryptionError):
        TokenCipher(_key()).decrypt(blob)


def test_from_settings_validates_key_length() -> None:
    short = SecretStr(base64.b64encode(b"too short").decode())
    with pytest.raises(ValueError, match="32 bytes"):
        TokenCipher.from_settings(AuthSettings(token_encryption_key=short))
    assert (
        TokenCipher.from_settings(AuthSettings()).decrypt(
            TokenCipher.from_settings(AuthSettings()).encrypt(b"ok")
        )
        == b"ok"
    )


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_dev_key_is_refused_outside_dev(env: str) -> None:
    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
        Settings(env=env)  # pyright: ignore[reportArgumentType]
    real = SecretStr(base64.b64encode(_key()).decode())
    assert Settings(env=env, auth=AuthSettings(token_encryption_key=real)).env == env  # pyright: ignore[reportArgumentType]


def test_dev_key_is_allowed_in_dev_and_test() -> None:
    for env in ("dev", "test"):
        settings = Settings(env=env)  # pyright: ignore[reportArgumentType]
        assert settings.auth.token_encryption_key.get_secret_value() == DEV_TOKEN_ENCRYPTION_KEY


def test_secure_cookies_default_off_only_in_dev() -> None:
    assert Settings(env="dev").secure_cookies is False
    assert Settings(env="test").secure_cookies is True
