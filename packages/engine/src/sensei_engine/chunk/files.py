"""A file's chunks, whatever it holds: code we parse by definition (:mod:`.code`), everything
else by its structure or in line windows (:mod:`.text`)."""

from __future__ import annotations

from collections.abc import Sequence

from sensei_engine.chunk.code import chunk_code
from sensei_engine.chunk.model import Chunk
from sensei_engine.chunk.text import chunk_text
from sensei_engine.ingest.classify import decode_text
from sensei_engine.parse.parser import ParseResult, parse_file
from sensei_engine.parse.stage import parseable
from sensei_engine.symbols.extract import Symbol, extract_symbols


def chunk_file(
    path: str,
    language: str | None,
    source: bytes | str,
    *,
    parsed: ParseResult | None = None,
    symbols: Sequence[Symbol] | None = None,
) -> list[Chunk]:
    """The chunks of the file at ``path`` (its language id, its content). A code file is parsed
    here unless ``parsed`` (and its ``symbols``) come with it; one that doesn't parse is chunked
    as text. A binary file has none."""
    if parseable(language) and language is not None:
        result = parsed if parsed is not None else parse_file(path, source, language=language)
        if result.ok:
            found = symbols if symbols is not None else extract_symbols(result, language)
            return chunk_code(result, language, found)
    if isinstance(source, str):
        text = source
    else:
        decoded = decode_text(source)
        if decoded is None:
            return []
        text = decoded[0]
    return chunk_text(path, language, text)
