"""Rebuild ``imports/fastapi-template.tsv``: what every JavaScript and TypeScript import
of the fastapi-template fixture names, by the TypeScript compiler (tests never run this).

    uv run python packages/engine/tests/fixtures/build_import_labels.py \\
        --node node --typescript apps/web/node_modules/typescript

The statements are listed from the parse captures; what each names is the compiler's answer,
never the resolver's under test:

- the file: ``ts.resolveModuleName`` with the importing file's own tsconfig;
- the definition a named or default import reaches, through barrels: the checker's aliased
  symbol (``getAliasedSymbol``) and its declaration's name. ``export default <expression>`` and
  a JSON module have no named declaration: the label is the module;
- a package (nothing is installed, so the compiler finds none): a bare specifier naming a
  dependency the importer's nearest ``package.json`` declares, or one of Node's built-in modules
  (``require("module").builtinModules``), as ``node:<name>``;
- a file that isn't code (a stylesheet, an image): the file, which must exist.

A row the compiler can't answer and none of these rules covers stops the build.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, cast

from sensei_engine.imports.ecma.resolve import package_name
from sensei_engine.imports.ecma.statements import NAMESPACE, extract_imports
from sensei_engine.ingest.languages import detect_language
from sensei_engine.parse.parser import parse_file

FIXTURES = Path(__file__).parent
ARCHIVE = FIXTURES / "fastapi-template.tar.gz"
OUT = FIXTURES / "imports" / "fastapi-template.tsv"
CODE = frozenset({"javascript", "jsx", "typescript", "tsx"})

ORACLE = FIXTURES / "import_labels_oracle.cjs"


def _dependencies(root: Path, importer: str) -> set[str]:
    """What the importer's nearest ``package.json`` declares."""
    directory: str = posixpath.dirname(importer)
    while True:
        manifest = root / directory / "package.json"
        if manifest.is_file():
            declared: dict[str, Any] = json.loads(manifest.read_text())
            return set(declared.get("dependencies", {})) | set(declared.get("devDependencies", {}))
        if not directory:
            return set()
        directory = posixpath.dirname(directory)


def _label(
    row: dict[str, object], answers: dict[str, dict[str, Any]], root: Path, builtins: set[str]
) -> tuple[str, str, str]:
    key = f"{row['importer']}:{row['line']}:{row['source']}"
    resolved = answers["files"].get(key)
    symbol = answers["symbols"].get(f"{row['importer']}:{row['line']}:{row['local']}")
    named = row["local"] is not None and row["imported"] not in (None, NAMESPACE)
    answer = cast("dict[str, str | None] | None", symbol)
    if answer is not None and named and answer.get("file"):
        file, name = str(answer["file"]), answer.get("name")
        return ("symbol", file, name) if name else ("module", file, "-")
    if isinstance(resolved, str):
        return "module", resolved, "-"
    source = str(row["source"])
    if not source.startswith((".", "/")):
        package = package_name(source)[0]
        if package in _dependencies(root, str(row["importer"])):
            return "external", package, "-"
        builtin = package.removeprefix("node:")
        if builtin in builtins:
            return "external", f"node:{builtin}", "-"
    if source.startswith("/"):
        candidates = [f"frontend/{source[1:]}", f"frontend/public/{source[1:]}"]
    else:
        directory = posixpath.dirname(str(row["importer"]))
        candidates = [posixpath.normpath(posixpath.join(directory, source))]
    for candidate in candidates:
        if detect_language(candidate) not in CODE and (root / candidate).is_file():
            return "file", candidate, "-"
    msg = f"no answer for {key}"
    raise SystemExit(msg)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", default="node")
    parser.add_argument("--typescript", required=True, help="the typescript package's directory")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        with tarfile.open(ARCHIVE) as archive:
            archive.extractall(temp, filter="data")
        root = Path(temp).resolve() / "fastapi-template"
        rows: list[dict[str, object]] = []
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            relative = str(path.relative_to(root))
            language = detect_language(relative)
            if language not in CODE or "node_modules" in relative:
                continue
            parsed = parse_file(relative, path.read_bytes(), language=language)
            rows.extend(
                {
                    "importer": relative,
                    "line": statement.line,
                    "source": statement.source,
                    "local": statement.local,
                    "imported": statement.imported,
                }
                for statement in extract_imports(parsed)
            )
        rows_file, answers_file = Path(temp) / "rows.json", Path(temp) / "answers.json"
        rows_file.write_text(json.dumps(rows))
        typescript = str(Path(args.typescript).resolve())
        command = [args.node, str(ORACLE), typescript, str(root), str(rows_file), str(answers_file)]
        subprocess.run(command, check=True)  # noqa: S603
        answers = json.loads(answers_file.read_text())
        listed = subprocess.run(  # noqa: S603
            [args.node, "-p", "JSON.stringify(require('module').builtinModules)"],
            check=True,
            capture_output=True,
            text=True,
        )
        builtins = set(json.loads(listed.stdout))
        lines = ["importer\tline\tsource\tlocal\tkind\ttarget\tname"]
        for row in rows:
            kind, target, name = _label(row, answers, root, builtins)
            local = row["local"] if row["local"] is not None else "-"
            lines.append(
                f"{row['importer']}\t{row['line']}\t{row['source']}\t{local}\t{kind}\t{target}\t{name}"
            )
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT.name}: {len(lines) - 1} imports")


if __name__ == "__main__":
    main()
