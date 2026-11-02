"""The sign-in, kept in the OS keyring (macOS Keychain, Windows Credential Locker, the Secret
Service on Linux), filed under the server's origin. Never in a plain file.

What is kept: the access and refresh tokens, when the access token expires, its scopes, and the
endpoints to refresh and revoke it at (found at sign-in, so they aren't looked up again).

Refresh tokens rotate and a reused one revokes its whole family, so two ``sensei`` commands
refreshing at once would sign the user out. A refresh is done under a file lock, and the keyring
is read again inside it: the second command finds the first one's new tokens and uses them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from sensei_cli.errors import CliError

SERVICE = "repo-sensei"
EARLY = 60  # refresh this many seconds before the access token expires


@dataclass(slots=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    scopes: list[str]
    token_endpoint: str
    revocation_endpoint: str | None = None
    resource: str | None = None

    def fresh(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.expires_at - EARLY

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, raw: str) -> Tokens | None:
        try:
            data = json.loads(raw)
            return cls(
                access_token=str(data["access_token"]),
                refresh_token=str(data["refresh_token"]),
                expires_at=float(data["expires_at"]),
                scopes=[str(s) for s in cast("list[Any]", data.get("scopes") or [])],
                token_endpoint=str(data["token_endpoint"]),
                revocation_endpoint=data.get("revocation_endpoint"),
                resource=data.get("resource"),
            )
        except (ValueError, KeyError, TypeError):
            return None  # not ours, or from another version: as if signed out


class Keyring(Protocol):
    def get_password(self, service: str, username: str, /) -> str | None: ...
    def set_password(self, service: str, username: str, password: str, /) -> None: ...
    def delete_password(self, service: str, username: str, /) -> None: ...


NO_KEYRING = (
    "No system keyring is available to keep your sign-in in. Use an API key instead: set "
    "SENSEI_API_KEY (create one in your organization's settings)."
)


class Credentials:
    """One server's sign-in in the keyring."""

    def __init__(self, server: str, store: Keyring | None = None) -> None:
        self.server = server
        self._store: Keyring = store if store is not None else keyring.get_keyring()

    def load(self) -> Tokens | None:
        try:
            raw = self._store.get_password(SERVICE, self.server)
        except KeyringError as error:
            raise CliError(NO_KEYRING) from error
        return Tokens.loads(raw) if raw else None

    def save(self, tokens: Tokens) -> None:
        try:
            self._store.set_password(SERVICE, self.server, tokens.dumps())
        except KeyringError as error:
            raise CliError(NO_KEYRING) from error

    def delete(self) -> None:
        try:
            self._store.delete_password(SERVICE, self.server)
        except PasswordDeleteError:
            pass  # already gone
        except KeyringError as error:
            raise CliError(NO_KEYRING) from error


@contextmanager
def refresh_lock(folder: Path) -> Generator[None]:
    """Held while refreshing, across ``sensei`` processes (POSIX; elsewhere, none)."""
    try:
        import fcntl  # noqa: PLC0415 - POSIX only
    except ImportError:  # pragma: no cover - Windows
        yield
        return
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / "refresh.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
