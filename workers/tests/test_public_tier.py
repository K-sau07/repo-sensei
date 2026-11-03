"""A public repository imported by its URL (the free tier, 10.1) stops at discovery when it has
more lines than the tier takes; a connected one doesn't."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

from sensei_workers.tasks.discover import Target, _too_large  # pyright: ignore[reportPrivateUsage]


def _target(max_loc: int | None) -> Target:
    return Target(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "a" * 40, (), max_loc)


def _files(*lines: int | None, language: str = "python", flags: tuple[str, ...] = ()) -> Any:
    return cast("Any", [SimpleNamespace(loc=n, language=language, flags=flags) for n in lines])


def test_a_public_repository_over_the_tiers_lines_is_stopped() -> None:
    assert _too_large(_target(100), _files(60, 40, None)) is None
    said = _too_large(_target(100), _files(60, 41))
    assert said is not None
    assert said.startswith("The repository has 101 lines to index; a public repository")
    assert _too_large(_target(None), _files(10**9)) is None  # connected: no cap


def test_only_code_counts_as_github_counts_it() -> None:
    # The import's estimate is GitHub's bytes of code: lockfiles, data, generated and vendored
    # files aren't in it, nor here.
    code = _files(90)
    lock = _files(10_000, language="json")
    vendored = _files(10_000, flags=("vendored",))
    assert _too_large(_target(100), [*code, *lock, *vendored]) is None
