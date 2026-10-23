"""Symbol extraction: keys, kinds, spans, signatures, docstrings and exports for Python and
TypeScript/TSX, keys that survive moved lines, and a sweep of the fastapi-template fixture."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from sensei_engine.ingest.languages import detect_language
from sensei_engine.parse.parser import parse_file
from sensei_engine.parse.stage import parseable
from sensei_engine.symbols.extract import Symbol, SymbolKind, extract_symbols
from sensei_engine.symbols.keys import Descriptor, KeyAllocator, descriptor, escape, module_key

ARCHIVE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"

PYTHON = '''"""Models."""

import functools

__all__ = ["Item", "make"]

LIMIT: int = 10  # the limit


# An item.
@functools.total_ordering
class Item(Base, metaclass=Meta):
    """One item.

    With details.
    """

    name: str = "x"

    @property
    def size(self) -> int:  # how big
        return 1

    @size.setter
    def size(self, value: int) -> None:
        pass

    async def load(
        self,
        key: str,  # the key
    ) -> "Item":
        def inner():
            return 2

        return self


# Make one.
# Or two.
def make(a, b=1, *args, **kw):
    return Item()


type Pair = tuple[int, int]
'''

TSX = """import React, { memo, forwardRef } from "react";

/**
 * Props of the table.
 */
export interface ItemsProps {
  items: string[];
}

export type Mode = "grid" | "list";

export enum Color { Red, Blue }

// Count the items.
export function useItemCount(items: string[]): number {
  return items.length;
}

export const ItemsTable = ({ items }: ItemsProps) => {
  const handleClick = () => console.log("hé");
  return <ul onClick={handleClick}>{items.map((i) => <li key={i}>{i}</li>)}</ul>;
};

export const Fancy = memo(function Fancy() {
  return <div />;
});

function Formatter(value: number): string {
  return String(value);
}

export class Board extends React.Component<ItemsProps> {
  @observable count = 0;

  render() {
    return <div>{this.count}</div>;
  }
}

export function overload(a: string): string;
export function overload(a: number): number;
export function overload(a: any): any { return a; }

export namespace Utils {}

export default function () {
  return <main />;
}

const a = 1, b = 2;
export { Formatter as Format };
"""


def _extract(path: str, source: str, language: str) -> dict[str, Symbol]:
    result = parse_file(path, source, language=language)
    assert result.ok
    symbols = extract_symbols(result, language)
    keys = [s.symbol_key for s in symbols]
    assert len(keys) == len(set(keys))
    return {s.symbol_key: s for s in symbols}


@pytest.fixture(scope="module")
def python() -> dict[str, Symbol]:
    return _extract("pkg/models.py", PYTHON, "python")


@pytest.fixture(scope="module")
def tsx() -> dict[str, Symbol]:
    return _extract("src/components/Items.tsx", TSX, "tsx")


# ---------------------------------------------------------------------------- keys


def test_names_that_arent_identifiers_are_quoted() -> None:
    assert escape("read_items") == "read_items"
    assert escape("$store") == "$store"
    assert escape("my name") == "`my name`"
    assert escape("a`b") == "`a``b`"
    assert descriptor("Item", Descriptor.TYPE) == "Item#"
    assert descriptor("get", Descriptor.CALLABLE) == "get()."
    assert descriptor("x", Descriptor.VALUE) == "x."
    assert module_key("typescript", "src/a.ts") == "ts src/a.ts/"


def test_repeated_keys_are_numbered_in_order() -> None:
    keys = KeyAllocator("python", "a.py")
    assert keys.module == "py a.py/"
    first = keys.allocate(keys.module, "f().")
    assert first == "py a.py/f()."
    assert keys.allocate(keys.module, "f().") == "py a.py/f().~1"
    assert keys.allocate(keys.module, "f().") == "py a.py/f().~2"
    assert keys.allocate(first, "g().") == "py a.py/f().g()."


# ---------------------------------------------------------------------------- python


def test_python_definitions_get_keys_kinds_and_qualified_names(
    python: dict[str, Symbol],
) -> None:
    found = [(s.symbol_key, s.kind, s.qualified_name) for s in python.values()]
    assert found == [
        ("py pkg/models.py/", SymbolKind.MODULE, "pkg.models"),
        ("py pkg/models.py/__all__.", SymbolKind.VARIABLE, "__all__"),
        ("py pkg/models.py/LIMIT.", SymbolKind.VARIABLE, "LIMIT"),
        ("py pkg/models.py/Item#", SymbolKind.CLASS, "Item"),
        ("py pkg/models.py/Item#name.", SymbolKind.VARIABLE, "Item.name"),
        ("py pkg/models.py/Item#size().", SymbolKind.METHOD, "Item.size"),
        ("py pkg/models.py/Item#size().~1", SymbolKind.METHOD, "Item.size"),  # the setter
        ("py pkg/models.py/Item#load().", SymbolKind.METHOD, "Item.load"),
        ("py pkg/models.py/Item#load().inner().", SymbolKind.FUNCTION, "Item.load.inner"),
        ("py pkg/models.py/make().", SymbolKind.FUNCTION, "make"),
        ("py pkg/models.py/Pair#", SymbolKind.CLASS, "Pair"),
    ]
    assert python["py pkg/models.py/Item#load().inner()."].container_key == (
        "py pkg/models.py/Item#load()."
    )
    assert python["py pkg/models.py/"].container_key is None
    assert python["py pkg/models.py/make()."].container_key == "py pkg/models.py/"


def test_python_spans_include_decorators(python: dict[str, Symbol]) -> None:
    item = python["py pkg/models.py/Item#"]
    assert (item.start_line, item.end_line) == (11, 35)  # from @functools.total_ordering
    assert (item.start_col, item.end_col) == (0, 19)
    size = python["py pkg/models.py/Item#size()."]
    assert (size.start_line, size.end_line, size.start_col) == (20, 22, 4)
    module = python["py pkg/models.py/"]
    assert (module.start_line, module.end_line) == (1, 44)


def test_python_signatures_stop_at_the_body_without_comments(python: dict[str, Symbol]) -> None:
    signatures = {key.removeprefix("py pkg/models.py/"): s.signature for key, s in python.items()}
    assert signatures == {
        "": None,
        "__all__.": '__all__ = ["Item", "make"]',
        "LIMIT.": "LIMIT: int = 10",
        "Item#": "class Item(Base, metaclass=Meta)",
        "Item#name.": 'name: str = "x"',
        "Item#size().": "def size(self) -> int",
        "Item#size().~1": "def size(self, value: int) -> None",
        "Item#load().": 'async def load(self, key: str) -> "Item"',
        "Item#load().inner().": "def inner()",
        "make().": "def make(a, b=1, *args, **kw)",
        "Pair#": "type Pair = tuple[int, int]",
    }


def test_python_docstrings_and_leading_comments(python: dict[str, Symbol]) -> None:
    assert python["py pkg/models.py/"].docstring == "Models."
    # The docstring wins over the comment above.
    assert python["py pkg/models.py/Item#"].docstring == "One item.\n\nWith details."
    assert python["py pkg/models.py/make()."].docstring == "Make one.\nOr two."
    assert python["py pkg/models.py/LIMIT."].docstring is None  # a trailing comment isn't one
    assert python["py pkg/models.py/Item#size()."].docstring is None


def test_python_attrs_record_decorators_bases_async_and_exports(
    python: dict[str, Symbol],
) -> None:
    assert python["py pkg/models.py/Item#"].attrs == {
        "decorators": ["@functools.total_ordering"],
        "bases": ["Base"],  # metaclass= isn't a base
        "exported": True,  # listed in __all__
    }
    assert python["py pkg/models.py/Item#size().~1"].attrs == {"decorators": ["@size.setter"]}
    assert python["py pkg/models.py/Item#load()."].attrs == {"async": True}
    assert python["py pkg/models.py/Item#name."].attrs == {"subkind": "field"}
    assert python["py pkg/models.py/make()."].attrs == {"exported": True}
    assert python["py pkg/models.py/Pair#"].attrs == {"subkind": "type"}
    assert "exported" not in python["py pkg/models.py/LIMIT."].attrs


def test_a_package_module_is_named_after_its_directory() -> None:
    symbols = _extract("app/core/__init__.py", "X = 1\n", "python")
    module = symbols["py app/core/__init__.py/"]
    assert (module.name, module.qualified_name) == ("core", "app.core.__init__")
    assert _extract("src/ui/index.ts", "", "typescript")["ts src/ui/index.ts/"].name == "ui"


# ---------------------------------------------------------------------------- typescript


def test_tsx_definitions_get_keys_and_kinds(tsx: dict[str, Symbol]) -> None:
    found = [
        (key.removeprefix("tsx src/components/Items.tsx/"), s.kind, s.attrs.get("subkind"))
        for key, s in tsx.items()
    ]
    assert found == [
        ("", SymbolKind.MODULE, None),
        ("ItemsProps#", SymbolKind.CLASS, "interface"),
        ("ItemsProps#items.", SymbolKind.VARIABLE, "field"),
        ("Mode#", SymbolKind.CLASS, "type"),
        ("Color#", SymbolKind.CLASS, "enum"),
        ("useItemCount().", SymbolKind.HOOK, None),
        ("ItemsTable().", SymbolKind.COMPONENT, None),  # an arrow function rendering JSX
        ("ItemsTable().handleClick().", SymbolKind.FUNCTION, None),
        ("Fancy().", SymbolKind.COMPONENT, None),  # memo(...)
        ("Formatter().", SymbolKind.FUNCTION, None),  # capitalized, but no JSX
        ("Board#", SymbolKind.COMPONENT, "class_component"),
        ("Board#count.", SymbolKind.VARIABLE, "field"),
        ("Board#render().", SymbolKind.METHOD, None),
        ("overload().", SymbolKind.FUNCTION, "signature"),
        ("overload().~1", SymbolKind.FUNCTION, "signature"),
        ("overload().~2", SymbolKind.FUNCTION, None),  # the implementation
        ("Utils#", SymbolKind.MODULE, "namespace"),
        (
            "default().",
            SymbolKind.COMPONENT,
            None,
        ),  # export default function () { return <main /> }
        ("a.", SymbolKind.VARIABLE, None),
        ("b.", SymbolKind.VARIABLE, None),
    ]


def test_tsx_spans_include_export_and_const(tsx: dict[str, Symbol]) -> None:
    table = tsx["tsx src/components/Items.tsx/ItemsTable()."]
    assert (table.start_line, table.end_line, table.start_col, table.end_col) == (19, 22, 0, 2)
    # Columns count characters: "é" is two bytes.
    click = tsx["tsx src/components/Items.tsx/ItemsTable().handleClick()."]
    assert (click.start_line, click.start_col, click.end_col) == (20, 2, 46)
    # Two declarators share a const: each spans only itself.
    b = tsx["tsx src/components/Items.tsx/b."]
    assert (b.start_line, b.start_col, b.end_col) == (50, 13, 18)
    count = tsx["tsx src/components/Items.tsx/Board#count."]
    assert count.attrs["decorators"] == ["@observable"]


def test_tsx_signatures_and_jsdoc(tsx: dict[str, Symbol]) -> None:
    def signature(name: str) -> str | None:
        return tsx[f"tsx src/components/Items.tsx/{name}"].signature

    assert signature("useItemCount().") == "function useItemCount(items: string[]): number"
    assert signature("ItemsTable().") == "const ItemsTable = ({items}: ItemsProps) =>"
    assert signature("Board#") == "class Board extends React.Component<ItemsProps>"
    assert signature("Board#render().") == "render()"
    assert signature("Mode#") == 'type Mode = "grid" | "list"'
    assert signature("overload().") == "function overload(a: string): string"
    assert signature("ItemsProps#items.") == "items: string[]"
    docs = tsx["tsx src/components/Items.tsx/ItemsProps#"].docstring
    assert docs == "Props of the table."
    assert tsx["tsx src/components/Items.tsx/useItemCount()."].docstring == "Count the items."


def test_tsx_exports(tsx: dict[str, Symbol]) -> None:
    def attrs(name: str) -> dict[str, object]:
        return tsx[f"tsx src/components/Items.tsx/{name}"].attrs

    assert attrs("ItemsTable().") == {"exported": True}
    assert attrs("default().") == {"exported": True, "default_export": True}
    assert attrs("Formatter().") == {"exported": True, "export_alias": "Format"}
    assert attrs("Board#") == {
        "subkind": "class_component",
        "bases": ["React.Component"],
        "exported": True,
    }
    assert "exported" not in attrs("a.")


def test_hooks_need_a_capital_after_use_and_components_need_jsx() -> None:
    source = (
        "export function useThing() { return 1; }\n"
        "export function user() { return 1; }\n"
        "export function Page() { return null; }\n"
    )
    symbols = _extract("src/a.jsx", source, "jsx")
    kinds = {s.name: s.kind for s in symbols.values()}
    assert kinds == {
        "a": SymbolKind.MODULE,
        "useThing": SymbolKind.HOOK,
        "user": SymbolKind.FUNCTION,
        "Page": SymbolKind.FUNCTION,  # capitalized, but renders no JSX
    }
    # Without JSX support (a .ts file), nothing is a component.
    plain = _extract("src/a.ts", "export const Page = () => 1;\n", "typescript")
    assert plain["ts src/a.ts/Page()."].kind is SymbolKind.FUNCTION


# ---------------------------------------------------------------------------- stability


def test_keys_survive_moved_lines_and_hashes_follow_content() -> None:
    before = _extract(
        "a.py", "def f():\n    return 1\n\n\nclass C:\n    def m(self): ...\n", "python"
    )
    after = _extract(
        "a.py",
        "import os\n\n\nclass C:\n    def m(self): ...\n\n\ndef f():\n    return 1\n",
        "python",
    )
    assert set(before) == set(after)
    for key in ("py a.py/f().", "py a.py/C#", "py a.py/C#m()."):
        assert before[key].content_hash == after[key].content_hash
        assert before[key].start_line != after[key].start_line
    assert before["py a.py/"].content_hash != after["py a.py/"].content_hash


def test_a_file_that_didnt_parse_has_no_symbols() -> None:
    result = parse_file("a.bin", b"\x00\x01\x02", language="python")
    assert not result.ok
    assert extract_symbols(result, "python") == []


def test_every_fixture_file_extracts_consistent_symbols() -> None:
    """Every source file of the fastapi-template fixture: unique keys, containers that exist,
    spans inside the file, and the keys the reference documents."""
    keys: set[str] = set()
    with tarfile.open(ARCHIVE) as archive:
        for member in archive.getmembers():
            handle = archive.extractfile(member) if member.isfile() else None
            if handle is None:
                continue
            path = member.name.split("/", 1)[1]
            data = handle.read()
            language = detect_language(path, data[:256])
            if language is None or not parseable(language):
                continue
            result = parse_file(path, data, language=language)
            symbols = extract_symbols(result, language)
            assert symbols, path
            module = symbols[0]
            known = {s.symbol_key for s in symbols}
            assert len(known) == len(symbols), path
            for symbol in symbols[1:]:
                assert symbol.container_key in known, (path, symbol.symbol_key)
                assert 1 <= symbol.start_line <= symbol.end_line <= module.end_line, symbol
            keys |= known
    assert {
        "py backend/app/api/routes/items.py/read_items().",
        "py backend/app/models.py/Item#",
        "tsx frontend/src/components/Items/AddItem.tsx/AddItem().",
        "tsx frontend/src/components/Items/AddItem.tsx/AddItem().onSubmit().",
    } <= keys
