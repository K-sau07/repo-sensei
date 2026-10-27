"""Rebuild ``imports/fastapi-template-code.tsv``: what every call, JSX element and prop callback
in the fastapi-template fixture's JavaScript and TypeScript refers to, by the TypeScript compiler
(tests never run this).

    uv run python packages/engine/tests/fixtures/build_code_labels.py \\
        --node node --typescript apps/web/node_modules/typescript

``code_labels_oracle.cjs`` walks every file and asks the checker what each called name, JSX tag
name and prop's name refers to (through imports and barrels: ``getAliasedSymbol``). A row is a
use whose declaration is in the repository: the file and line of the use, the name used, the
declaring file and the declared name. The resolver under test is never asked.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

from sensei_engine.ingest.languages import detect_language

FIXTURES = Path(__file__).parent
ARCHIVE = FIXTURES / "fastapi-template.tar.gz"
ORACLE = FIXTURES / "code_labels_oracle.cjs"
OUT = FIXTURES / "imports" / "fastapi-template-code.tsv"
CODE = frozenset({"javascript", "jsx", "typescript", "tsx"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", default="node")
    parser.add_argument("--typescript", required=True, help="the typescript package's directory")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        with tarfile.open(ARCHIVE) as archive:
            archive.extractall(temp, filter="data")
        root = Path(temp).resolve() / "fastapi-template"
        files = sorted(
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file() and detect_language(str(path.relative_to(root))) in CODE
        )
        files_file, answers_file = Path(temp) / "files.json", Path(temp) / "answers.json"
        files_file.write_text(json.dumps(files))
        typescript = str(Path(args.typescript).resolve())
        command = [
            args.node,
            str(ORACLE),
            typescript,
            str(root),
            str(files_file),
            str(answers_file),
        ]
        subprocess.run(command, check=True)  # noqa: S603
        rows = sorted({tuple(row) for row in json.loads(answers_file.read_text())})
    lines = ["file\tline\tname\ttarget\tdeclared"]
    lines.extend("\t".join(str(part) for part in row) for row in rows)
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT.name}: {len(rows)} uses")


if __name__ == "__main__":
    main()
