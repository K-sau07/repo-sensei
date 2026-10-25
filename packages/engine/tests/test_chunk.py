"""Chunking: code by definition (cAST), its context headers, Markdown, SQL, OpenAPI and
configuration by their structure, other files in windows; and on the whole fastapi-template
fixture, sizes within the cap, every function covered, the same chunks every time."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import statistics
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from sensei_engine.chunk.files import chunk_file
from sensei_engine.chunk.identifiers import index_terms
from sensei_engine.chunk.model import MAX_TOKENS, Chunk, ChunkKind, Lines, split_lines
from sensei_engine.chunk.text import WINDOW_LINES, WINDOW_OVERLAP
from sensei_engine.chunk.tokens import count_tokens
from sensei_engine.ingest.languages import detect_language
from sensei_engine.parse.parser import parse_file
from sensei_engine.parse.stage import parseable
from sensei_engine.symbols.extract import SymbolKind, extract_symbols

ARCHIVE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"
CALLABLE = {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.COMPONENT, SymbolKind.HOOK}


def _body(indent: str, count: int = 12) -> str:
    return "\n".join(
        f"{indent}value_{i} = transform(path, option_{i}, retries={i})" for i in range(count)
    )


PYTHON = f'''"""Helpers."""

import os
from pathlib import Path

LIMIT = 3


def small_one():
    return os.sep


def small_two():
    return Path(".")


def big_one(path):
    """Do a lot."""
{_body("    ")}
    return path


class Store(Base):
    """Keeps things."""

    name: str = "store"
    size: int = 0

    # Load it.
    @cached
    def load(self, key):
{_body("        ")}
        return key

    @property
    def count(self):
        return self.size

    def save(self):
{_body("        ")}
        return None
'''


def _summary(chunks: list[Chunk]) -> list[tuple[str, int, int, str | None]]:
    return [(c.kind.value, c.start_line, c.end_line, c.symbol_key) for c in chunks]


# ---------------------------------------------------------------------- code


def test_definitions_are_the_units_and_small_neighbours_merge() -> None:
    chunks = chunk_file("pkg/helpers.py", "python", PYTHON)
    assert _summary(chunks) == [
        ("code", 1, 14, None),  # the imports, LIMIT and two small helpers, together
        ("function", 17, 31, "py pkg/helpers.py/big_one()."),
        ("class", 34, 38, "py pkg/helpers.py/Store#"),  # its header: docstring and fields
        ("method", 40, 55, "py pkg/helpers.py/Store#load()."),  # with the comment above it
        ("method", 57, 59, "py pkg/helpers.py/Store#count()."),
        ("method", 61, 74, "py pkg/helpers.py/Store#save()."),
    ]
    assert chunks[0].header == (
        "# path: pkg/helpers.py\n"
        "# lang: python · module: pkg.helpers\n"
        "# doc: Helpers.\n"
        "# defines: LIMIT, small_one, small_two\n"
        "# uses: os, Path"
    )
    assert chunks[3].header == (
        "# path: pkg/helpers.py\n"
        "# lang: python · method: Store.load\n"
        "# signature: def load(self, key)\n"
        "# doc: Load it.\n"
        "# decorators: @cached"
    )
    assert chunks[3].content.startswith("    # Load it.\n    @cached\n    def load(self, key):")
    assert chunks[2].content.endswith("size: int = 0")


def test_a_class_without_methods_and_a_tiny_class_are_whole() -> None:
    source = (
        "class Point:\n"
        "    x: int\n"
        "    y: int\n"
        "\n"
        "\n"
        "class Tiny:\n"
        "    def get(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "X = 1\n"
    )
    chunks = chunk_file("geo.py", "python", source)
    assert _summary(chunks) == [
        ("class", 1, 3, "py geo.py/Point#"),
        ("class", 6, 8, "py geo.py/Tiny#"),  # a class is never merged with its neighbours
        ("variable", 11, 11, "py geo.py/X."),
    ]


def test_a_huge_function_is_split_at_statements_with_its_signature_in_every_part() -> None:
    body = "\n".join(
        f"    x{i} = compute_value_{i}(alpha, beta, gamma, delta)\n"
        f"    if x{i} > {i}:\n"
        f"        total += x{i}"
        for i in range(120)
    )
    source = f'def huge(alpha, beta, gamma, delta):\n    """Add up."""\n    total = 0\n{body}\n'
    chunks = chunk_file("big.py", "python", source)
    count = len(chunks)
    assert count > 2
    assert [c.part for c in chunks] == [(k, count) for k in range(1, count + 1)]
    assert {c.symbol_key for c in chunks} == {"py big.py/huge()."}
    assert {c.kind for c in chunks} == {ChunkKind.FUNCTION}
    # Consecutive, together the whole function.
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == len(source.splitlines())
    for before, after in itertools.pairwise(chunks):
        assert after.start_line == before.end_line + 1
    lines = source.splitlines()
    for k, chunk in enumerate(chunks, 1):
        assert chunk.tokens <= MAX_TOKENS
        assert "# signature: def huge(alpha, beta, gamma, delta)" in chunk.header
        assert chunk.header.endswith(f"# part: {k}/{count}")
        if k > 1:  # each part starts at a statement of the body, not inside the if
            assert re.match(r"    (x\d+ =|if )", lines[chunk.start_line - 1])


def test_comments_belong_to_the_definition_below_and_scraps_to_a_neighbour() -> None:
    fields = "\n".join(
        f'      <Field name="field_{i}" label="Field {i}" onChange={{handleChange}} />'
        for i in range(14)
    )
    source = (
        'import { useState } from "react"\n'
        'import { Field } from "@/components/Field"\n'
        "\n"
        "// The settings form.\n"
        "export function SettingsForm({ onSave }: Props) {\n"
        '  const [value, setValue] = useState("")\n'
        "  const handleChange = (next: string) => setValue(next)\n"
        "  return (\n"
        "    <form onSubmit={() => onSave(value)}>\n"
        f"{fields}\n"
        "    </form>\n"
        "  )\n"
        "}\n"
        "\n"
        "export default SettingsForm\n"
    )
    (chunk,) = chunk_file("web/src/SettingsForm.tsx", "tsx", source)
    # The imports and the default export are too small to stand alone.
    assert (chunk.kind, chunk.start_line, chunk.end_line) == (ChunkKind.COMPONENT, 1, 28)
    assert chunk.symbol_key == "tsx web/src/SettingsForm.tsx/SettingsForm()."
    assert chunk.header == (
        "# path: web/src/SettingsForm.tsx\n"
        "# lang: tsx · component: SettingsForm · exports: SettingsForm (default)\n"
        "# signature: function SettingsForm({onSave}: Props)\n"
        "# doc: The settings form.\n"
        "# uses: useState, Field"
    )


def test_search_and_path_text() -> None:
    chunks = chunk_file("pkg/helpers.py", "python", PYTHON)
    load = chunks[3]
    assert load.search_text == index_terms(f"{load.header}\n{load.content}")
    terms = load.search_text.split()
    assert {"value0", "value", "0", "option11", "transform", "cached"} <= set(terms)
    assert terms[:3] == ["path", "pkg", "helpers"]  # the header first
    assert load.path_text == "pkg helpers py def load self key load"
    # Several small definitions: their names.
    assert chunks[0].path_text == "pkg helpers py limit smallone small one smalltwo small two"


def test_chunks_are_deterministic() -> None:
    first = chunk_file("pkg/helpers.py", "python", PYTHON)
    again = chunk_file("pkg/helpers.py", "python", PYTHON.encode())
    assert first == again
    assert len({c.content_hash for c in first}) == len(first)
    # The hash covers the header: the same code elsewhere is another chunk.
    moved = chunk_file("pkg/other.py", "python", PYTHON)
    assert first[1].content == moved[1].content
    assert first[1].content_hash != moved[1].content_hash


def test_code_that_doesnt_parse_is_chunked_as_text() -> None:
    result = parse_file("a.py", "x = 1\n", language="python", timeout_seconds=-1)
    assert not result.ok
    (chunk,) = chunk_file("a.py", "python", "x = 1\n", parsed=result)
    assert chunk.kind is ChunkKind.TEXT
    assert chunk_file("logo.png", None, b"\x89PNG\r\n\x1a\n\x00\x00") == []
    assert chunk_file("empty.py", "python", "") == []


def test_split_lines_prefers_the_outermost_boundary_that_fills_a_piece() -> None:
    lines = Lines("\n".join(f"word{i} " * 10 for i in range(20)))
    per_line = lines.tokens(1, 1)
    # Any line may end a piece: pieces are as full as the budget allows.
    pieces = split_lines(lines, 1, 20, per_line * 6)
    assert pieces == [(1, 6), (7, 12), (13, 18), (19, 20)]
    # Line 5 is the only shallow boundary in reach: the first piece ends there.
    cuts = {5: 1, 6: 3, 7: 3, 11: 1, 12: 3}
    assert split_lines(lines, 1, 20, per_line * 6, cuts)[0] == (1, 4)
    # A boundary that leaves the piece under half full isn't preferred for being shallow.
    assert split_lines(lines, 1, 20, per_line * 6, {2: 0, 6: 2})[0] == (1, 5)


# ---------------------------------------------------------------------- text


def _paragraph(word: str, sentences: int = 12) -> str:
    return " ".join(f"The {word} step {i} explains one thing in detail." for i in range(sentences))


def test_markdown_is_chunked_by_heading() -> None:
    source = (
        "---\ntitle: Guide\n---\n\n"
        f"{_paragraph('intro')}\n\n"
        "# Install\n\n"
        f"{_paragraph('install')}\n\n"
        "```bash\n# not a heading\npip install sensei\n```\n\n"
        "## Docker\n\n"
        f"{_paragraph('docker')}\n\n"
        "Setup\n=====\n\n"
        f"{_paragraph('setup')}\n"
    )
    chunks = chunk_file("docs/guide.md", "markdown", source)
    headers = [c.header.splitlines()[1] for c in chunks]
    assert headers == [
        "# lang: markdown · section: front matter",
        "# lang: markdown",  # the text before the first heading
        "# lang: markdown · section: Install",
        "# lang: markdown · section: Install > Docker",
        "# lang: markdown · section: Setup",  # a setext heading
    ]
    install = chunks[2]
    assert install.content.startswith("# Install")
    assert "# not a heading" in install.content  # a comment in a code block isn't a heading
    assert {c.kind for c in chunks} == {ChunkKind.DOC}
    assert chunks[0].start_line == 1


def test_sql_is_chunked_by_statement() -> None:
    table = ",\n".join(f"  column_{i} text default 'a;b'" for i in range(30))
    source = (
        "-- Users\n"
        f"CREATE TABLE users (\n{table}\n);\n"
        "\n"
        "CREATE FUNCTION f() RETURNS int AS $$ select 1; $$ LANGUAGE sql;\n"
        "INSERT INTO users (name) VALUES ('x'); INSERT INTO users (name) VALUES ('y');\n"
    )
    chunks = chunk_file("db/schema.sql", "sql", source)
    assert [(c.kind, c.start_line, c.end_line) for c in chunks] == [
        (ChunkKind.SQL, 1, 33),  # the comment above, and ";" in strings, don't split it
        (ChunkKind.SQL, 35, 36),  # small statements together; $$ bodies stay whole
    ]
    assert chunks[0].header.splitlines()[1] == "# lang: sql · statement: CREATE TABLE users"
    assert chunks[1].header.splitlines()[2] == (
        "# statements: CREATE FUNCTION f, INSERT INTO users, INSERT INTO users"
    )


SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "API", "version": "1"},
    "paths": {
        "/items/{id}": {
            "get": {
                "operationId": "read_item",
                "summary": "Read an item",
                "responses": {"200": {"description": "ok"}},
            },
            "delete": {"operationId": "delete_item", "responses": {"204": {}}},
        },
        "/items": {"post": {"operationId": "create_item", "responses": {}}},
    },
    "components": {"schemas": {"Item": {"type": "object"}}},
}


@pytest.mark.parametrize(
    ("path", "language", "source"),
    [
        ("openapi.json", "json", json.dumps(SPEC, indent=2)),
        ("openapi.yaml", "yaml", yaml.safe_dump(SPEC, sort_keys=False)),
    ],
)
def test_openapi_is_chunked_by_operation(path: str, language: str, source: str) -> None:
    chunks = chunk_file(path, language, source)
    operations = [c for c in chunks if c.kind is ChunkKind.OPENAPI_OPERATION]
    assert [c.header.splitlines()[1:] for c in operations] == [
        [
            f"# lang: {language} · operation: GET /items/{{id}}",
            "# operationId: read_item",
            "# summary: Read an item",
        ],
        [f"# lang: {language} · operation: DELETE /items/{{id}}", "# operationId: delete_item"],
        [f"# lang: {language} · operation: POST /items", "# operationId: create_item"],
    ]
    assert '"read_item"' in operations[0].content or "read_item" in operations[0].content
    assert operations[0].path_text.endswith("readitem read item")
    assert {c.kind for c in chunks} == {ChunkKind.CONFIG, ChunkKind.OPENAPI_OPERATION}


def test_configuration_is_chunked_by_key_path() -> None:
    services = "\n".join(
        f"  service_{i}:\n    image: registry.example.com/team/service-{i}:latest\n"
        f"    environment:\n      - NAME=service-{i}\n      - PORT=80{i:02}\n"
        f"    command: run --worker {i} --queue default --concurrency 4"
        for i in range(12)
    )
    source = f"version: '3'\nservices:\n{services}\nvolumes:\n  data: {{}}\n"
    chunks = chunk_file("compose.yml", "yaml", source)
    labels = [c.header.splitlines()[2] for c in chunks]
    # services is over the target: its own keys are the units (merged while small).
    assert labels[0].startswith("# keys: version, services.service_0, services.service_1")
    assert labels[-1].endswith("services.service_11, volumes")
    assert all(c.kind is ChunkKind.CONFIG for c in chunks)
    assert len(chunks) > 1
    assert chunks[-1].content.endswith("data: {}")
    toml = '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["E"]\n'
    (chunk,) = chunk_file("pyproject.toml", "toml", toml)
    assert chunk.header.splitlines()[2] == "# keys: tool.ruff, tool.ruff.lint"
    # The last row of an array over several lines is no table.
    rows = '[tool.a]\nmatrix = [\n  [1, 2],\n  [3, 4]\n]\nafter = "[x]"\n\n[tool.b]\nx = 1\n'
    (chunk,) = chunk_file("pyproject.toml", "toml", rows)
    assert chunk.header.splitlines()[2] == "# keys: tool.a, tool.b"


def test_other_files_are_chunked_in_overlapping_windows() -> None:
    source = "".join(f"line {i}\n" for i in range(1, 131))
    chunks = chunk_file("notes.txt", "text", source)
    step = WINDOW_LINES - WINDOW_OVERLAP
    assert [(c.start_line, c.end_line) for c in chunks] == [
        (1, WINDOW_LINES),
        (1 + step, WINDOW_LINES + step),
        (1 + 2 * step, 130),
    ]
    assert {c.kind for c in chunks} == {ChunkKind.TEXT}
    # Invalid YAML falls back to windows too.
    (bad,) = chunk_file("bad.yaml", "yaml", "a: [1, 2\nb: {")
    assert bad.kind is ChunkKind.TEXT


def test_a_line_too_long_for_one_chunk_is_cut() -> None:
    source = "const data = '" + "ab12 " * 3000 + "';\n"
    chunks = chunk_file("data.js", "javascript", source)
    assert len(chunks) > 1
    assert all(c.tokens <= MAX_TOKENS for c in chunks)
    assert {(c.start_line, c.end_line) for c in chunks} == {(1, 1)}
    assert "".join(c.content for c in chunks) == source.rstrip("\n")
    # Cut by tokens, not characters: a line half words, half CJK fills its pieces just as well.
    mixed = "const s = '" + "value_7 " * 6000 + "漢字かな交じり文" * 2000 + "';\n"
    cut = chunk_file("mixed.js", "javascript", mixed)
    assert len(cut) <= math.ceil(count_tokens(mixed) / (MAX_TOKENS - 100))
    assert all(c.tokens <= MAX_TOKENS for c in cut)
    assert "".join(c.content for c in cut) == mixed.rstrip("\n")


def test_a_very_long_path_leaves_the_content_room() -> None:
    folders = "/".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(32))
    path = f"{folders}/module.py"
    assert len(path) > 2000
    assert count_tokens(path) > MAX_TOKENS  # alone, more than a chunk may hold
    source = "def run(value):\n" + _body("    ", 80) + "\n    return value\n"
    chunks = chunk_file(path, "python", source)
    assert len(chunks) > 1
    assert all(c.tokens <= MAX_TOKENS for c in chunks)
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == len(source.splitlines())
    assert chunks[0].header.startswith("# path: …")  # its end
    assert "/module.py\n" in chunks[0].header
    # A line too long for one chunk, cut under the same header.
    data = "const data = '" + "ab12 " * 3000 + "';\n"
    cut = chunk_file(f"{folders}/data.js", "javascript", data)
    assert "".join(c.content for c in cut) == data.rstrip("\n")
    assert all(c.tokens <= MAX_TOKENS for c in cut)


# ---------------------------------------------------------------------- the fixture


@pytest.fixture(scope="module")
def fixture_chunks() -> dict[str, tuple[str | None, bytes, list[Chunk]]]:
    found: dict[str, tuple[str | None, bytes, list[Chunk]]] = {}
    with tarfile.open(ARCHIVE) as archive:
        for member in archive.getmembers():
            handle = archive.extractfile(member) if member.isfile() else None
            if handle is None:
                continue
            path = member.name.split("/", 1)[1]
            data = handle.read()
            language = detect_language(path, data[:256])
            found[path] = (language, data, chunk_file(path, language, data))
    return found


def test_no_fixture_chunk_is_over_the_cap(
    fixture_chunks: dict[str, tuple[str | None, bytes, list[Chunk]]],
) -> None:
    sizes = [c.tokens for _, _, chunks in fixture_chunks.values() for c in chunks]
    assert len(sizes) > 1000
    assert max(sizes) <= MAX_TOKENS
    for _, _, chunks in fixture_chunks.values():
        for chunk in chunks:
            assert chunk.tokens == count_tokens(f"{chunk.header}\n{chunk.content}")
    assert 100 <= statistics.median(sizes) <= 500  # the target range


def test_fixture_code_is_covered_once_and_every_function_whole(
    fixture_chunks: dict[str, tuple[str | None, bytes, list[Chunk]]],
) -> None:
    functions = 0
    for path, (language, data, chunks) in fixture_chunks.items():
        if language is None or not parseable(language):
            continue
        text = data.decode()
        lines = text.split("\n")
        covered: list[int] = []
        for chunk in chunks:
            covered.extend(range(chunk.start_line, chunk.end_line + 1))
            first = lines[chunk.start_line - 1]
            assert chunk.content.split("\n", 1)[0] == first.removesuffix("\r"), path
        assert len(covered) == len(set(covered)), path  # no line in two chunks
        blank = {n for n, line in enumerate(lines, 1) if not line.strip()}
        expected = set(range(1, len(lines) + 1)) - blank
        assert expected <= set(covered), path
        result = parse_file(path, data, language=language)
        for symbol in extract_symbols(result, language):
            if symbol.kind not in CALLABLE:
                continue
            functions += 1
            # A function is inside one chunk (with neighbours, or alone), or its parts.
            holding = [
                c
                for c in chunks
                if c.start_line <= symbol.end_line and c.end_line >= symbol.start_line
            ]
            assert holding, (path, symbol.symbol_key)
            if len(holding) > 1:
                assert all(c.symbol_key is not None and c.part for c in holding), symbol
    assert functions > 300


def test_fixture_chunks_are_deterministic(
    fixture_chunks: dict[str, tuple[str | None, bytes, list[Chunk]]],
) -> None:
    for path in ("backend/app/api/routes/items.py", "frontend/src/routes/login.tsx", "README.md"):
        language, data, chunks = fixture_chunks[path]
        again = chunk_file(path, language, data)
        assert [c.content_hash for c in again] == [c.content_hash for c in chunks]
