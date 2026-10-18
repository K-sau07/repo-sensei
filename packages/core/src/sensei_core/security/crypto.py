"""Authenticated encryption for secrets stored at rest (OAuth tokens, integration secrets).

AES-256-GCM with a random 96-bit nonce. Ciphertexts are self-describing::

    b"v1" | key_id (4 bytes) | nonce (12 bytes) | ciphertext+tag

``key_id`` is derived from the key, so data encrypted with a previous key can still be read during
a rotation (configure it as ``SENSEI_AUTH_TOKEN_ENCRYPTION_KEY_PREVIOUS``).
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sensei_core.config import AuthSettings

VERSION = b"v1"
_KEY_ID_LEN = 4
_NONCE_LEN = 12


class DecryptionError(Exception):
    """The ciphertext is malformed, was tampered with, or was made with an unknown key."""


def _key_id(key: bytes) -> bytes:
    return hashlib.sha256(b"sensei-key-id:" + key).digest()[:_KEY_ID_LEN]


def _decode_key(encoded: str) -> bytes:
    key = base64.b64decode(encoded, validate=True)
    if len(key) != 32:
        msg = "encryption keys must be 32 bytes (base64 encoded)"
        raise ValueError(msg)
    return key


class TokenCipher:
    def __init__(self, current: bytes, previous: bytes | None = None) -> None:
        self._current = current
        self._keys = {_key_id(current): current}
        if previous is not None:
            self._keys[_key_id(previous)] = previous

    @classmethod
    def from_settings(cls, settings: AuthSettings) -> TokenCipher:
        previous = settings.token_encryption_key_previous
        return cls(
            _decode_key(settings.token_encryption_key.get_secret_value()),
            _decode_key(previous.get_secret_value()) if previous else None,
        )

    def encrypt(self, plaintext: bytes, *, context: bytes = b"") -> bytes:
        """Encrypt ``plaintext``. ``context`` is bound as associated data (e.g. a user id)."""
        nonce = os.urandom(_NONCE_LEN)
        sealed = AESGCM(self._current).encrypt(nonce, plaintext, context or None)
        return VERSION + _key_id(self._current) + nonce + sealed

    def decrypt(self, blob: bytes, *, context: bytes = b"") -> bytes:
        header = len(VERSION) + _KEY_ID_LEN
        if len(blob) < header + _NONCE_LEN + 16 or not blob.startswith(VERSION):
            msg = "malformed ciphertext"
            raise DecryptionError(msg)
        key = self._keys.get(blob[len(VERSION) : header])
        if key is None:
            msg = "ciphertext was encrypted with an unknown key"
            raise DecryptionError(msg)
        nonce, sealed = blob[header : header + _NONCE_LEN], blob[header + _NONCE_LEN :]
        try:
            return AESGCM(key).decrypt(nonce, sealed, context or None)
        except InvalidTag as exc:
            msg = "ciphertext failed authentication"
            raise DecryptionError(msg) from exc

    def encrypt_text(self, text: str, *, context: str = "") -> bytes:
        return self.encrypt(text.encode(), context=context.encode())

    def decrypt_text(self, blob: bytes, *, context: str = "") -> str:
        return self.decrypt(blob, context=context.encode()).decode()
