"""Write the web mocks' symbols (apps/web/src/mocks/fixtures/symbols.json).

The mock serves the content of a selection of fastapi/full-stack-fastapi-template's files at
b1125d8 (fixtures/browse-content.json); this extracts their symbols with the engine, exactly as
indexing does, in the shape ``GET …/symbols`` returns them (without the score, which the mock
computes per query). An API test checks the file still matches the extractor, so re-run this
after changing it::

    uv run python scripts/mock_symbols.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

from sensei_api.routers.symbols import SymbolOut
from sensei_engine.ingest.languages import detect_language
from sensei_engine.parse.parser import parse_file
from sensei_engine.parse.stage import parseable
from sensei_engine.symbols.extract import extract_symbols

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "apps" / "web" / "src" / "mocks" / "fixtures"
CONTENT = FIXTURES / "browse-content.json"
OUT = FIXTURES / "symbols.json"
NOTE = (
    "The symbols of the mock snapshot's files whose content is in browse-content.json, from the "
    "engine's extractor by scripts/mock_symbols.py, as GET /symbols returns them (without the "
    "score). Lines are 1-based and inclusive."
)


def build() -> dict[str, Any]:
    content = orjson.loads(CONTENT.read_bytes())
    symbols: list[dict[str, Any]] = []
    for path, text in sorted(content["files"].items()):
        source = text.encode()
        language = detect_language(path, source[:256])
        if language is None or not parseable(language):
            continue
        for symbol in extract_symbols(parse_file(path, source, language=language), language):
            out = SymbolOut(
                symbol_key=symbol.symbol_key,
                name=symbol.name,
                qualified_name=symbol.qualified_name,
                kind=symbol.kind,
                path=path,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                signature=symbol.signature,
                score=0,
            )
            symbols.append(out.model_dump(mode="json", exclude={"score"}))
    return {"source": content["source"], "note": NOTE, "symbols": symbols}


def main() -> None:
    OUT.write_bytes(orjson.dumps(build(), option=orjson.OPT_INDENT_2) + b"\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
