"""Golden files: what the defs, imports, calls and decorators queries capture in ten real files
of the fastapi-template fixture (Python, TypeScript and TSX), against snapshots checked by hand.

A snapshot line is ``<main capture> L<first>-<last> <role>=<text> ...`` (roles in name order,
whitespace collapsed, long texts cut; a call's arguments left out). After a deliberate query change,
regenerate with ``SENSEI_UPDATE_PARSE_SNAPSHOTS=1`` and check the diff line by line against the
source.
"""

from __future__ import annotations

import json
import os
import re
import tarfile
from pathlib import Path
from typing import Any

import pytest

from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import Match, ParseResult, ParseStatus, lines, parse_file

FIXTURES = Path(__file__).parent / "fixtures"
ARCHIVE = FIXTURES / "fastapi-template.tar.gz"
SNAPSHOTS = FIXTURES / "parse"
UPDATE = os.environ.get("SENSEI_UPDATE_PARSE_SNAPSHOTS") == "1"

SAMPLES = [
    "backend/app/api/routes/items.py",  # route decorators
    "backend/app/models.py",  # SQLModel classes and fields
    "backend/app/core/config.py",  # settings fields, validators, properties
    "backend/app/api/deps.py",  # Depends, Annotated aliases
    "frontend/src/hooks/useAuth.ts",  # a hook, re-export lists
    "frontend/src/utils.ts",
    "frontend/src/client/core/auth.gen.ts",  # generated client code, types
    "frontend/src/components/Items/AddItem.tsx",  # a component, JSX, a local function
    "frontend/src/main.tsx",
    "frontend/src/routes/login.tsx",  # route definitions, a component
]
KINDS = (QueryKind.DEFS, QueryKind.IMPORTS, QueryKind.CALLS, QueryKind.DECORATORS)
MAX_TEXT = 60


@pytest.fixture(scope="module")
def sources() -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    with tarfile.open(ARCHIVE) as archive:
        for path in SAMPLES:
            handle = archive.extractfile(f"fastapi-template/{path}")
            assert handle is not None
            found[path] = handle.read()
    return found


def _text(result: ParseResult, match: Match, role: str) -> str:
    text = " ".join(result.text(node) for node in match.roles[role])
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _line(result: ParseResult, match: Match) -> str:
    first, last = lines(match.node)
    roles = [
        f"{role}={_text(result, match, role)}"
        for role in sorted(match.roles)
        if not (role == "arguments" and match.kind.startswith("call"))
    ]
    return " ".join([match.kind, f"L{first}-{last}", *roles])


def snapshot(result: ParseResult) -> dict[str, Any]:
    return {
        "language": result.language,
        "status": result.status.value,
        "error_count": result.error_count,
        **{kind.value: [_line(result, m) for m in result.captures[kind]] for kind in KINDS},
    }


@pytest.mark.parametrize("path", SAMPLES)
def test_captures_match_the_snapshot(path: str, sources: dict[str, bytes]) -> None:
    result = parse_file(path, sources[path], kinds=KINDS)
    assert result.status is ParseStatus.PARSED
    actual = snapshot(result)
    file = SNAPSHOTS / (path.replace("/", "__") + ".json")
    if UPDATE:
        SNAPSHOTS.mkdir(exist_ok=True)
        file.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n")
    assert actual == json.loads(file.read_text())
