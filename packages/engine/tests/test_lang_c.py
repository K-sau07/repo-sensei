"""C: the queries (definitions, includes, calls, strings, references) and the symbols of a small
key-value store (its header and its source), keyed, spanned and described; names through
pointer and parenthesized declarators, typedefs, a prototype and its definition, locals, macros,
a file going through parsing, symbols and chunks end to end, and C's test files."""

from __future__ import annotations

import time

import pytest

from sensei_engine.chunk.code import imported_names
from sensei_engine.chunk.files import chunk_file
from sensei_engine.ingest.roles import assign_role
from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult, ParseStatus, parse_file
from sensei_engine.symbols.extract import Symbol, SymbolKind, extract_symbols

HEADER = """/* kvstore: a tiny in-memory key-value store. MIT licence. */
#ifndef KV_STORE_H
#define KV_STORE_H

#include <stddef.h>
#include "kv/config.h"

#define KV_MAX_KEY 64
#define KV_VERSION "1.2.0"
#define KV_MIN(a, b) ((a) < (b) ? (a) : (b))

/* What a lookup can come back with. */
typedef enum {
    KV_OK = 0,
    KV_MISSING,
    KV_FULL
} kv_status;

/** One entry of a bucket's chain. */
typedef struct kv_entry {
    char key[KV_MAX_KEY];
    void *value;
    struct kv_entry *next;
} kv_entry;

/* The operations a backend provides. */
struct kv_ops {
    int (*open)(const char *path);
    void (*close)(void);
};

typedef struct kv_store kv_store;
typedef int (*kv_visit_fn)(const kv_entry *entry, void *ctx);

union kv_value {
    long number;
    const char *text;
};

extern int kv_debug;

/* Make an empty store with room for n buckets. */
kv_store *kv_open(size_t buckets);
kv_status kv_put(kv_store *store, const char *key, void *value);
void *kv_get(const kv_store *store, const char *key);
int kv_each(kv_store *store, kv_visit_fn visit, void *ctx);
void kv_close(kv_store *store);

#endif /* KV_STORE_H */
"""

SOURCE = """#include "kv/store.h"

#include <stdlib.h>
#include <string.h>

struct kv_store {
    kv_entry **buckets;
    size_t count, size;
    const struct kv_ops *ops;
};

int kv_debug = 0;
static const char *const kv_names[] = {"ok", "missing", "full"};

static size_t hash(const char *key, size_t size);
static int (*pick_visitor(int verbose))(const kv_entry *, void *);

kv_store *kv_open(size_t buckets) {
    kv_store *store = calloc(1, sizeof *store);
    store->buckets = calloc(buckets, sizeof(kv_entry *));
    store->size = buckets;
    return store;
}

// Insert or replace the value under key.
kv_status kv_put(kv_store *store, const char *key, void *value) {
    size_t at = hash(key, store->size);
    kv_entry *entry = malloc(sizeof *entry);
    strncpy(entry->key, key, KV_MAX_KEY - 1);
    entry->value = value;
    entry->next = store->buckets[at];
    store->buckets[at] = entry;
    if (store->ops) {
        store->ops->open("kv.log");
    }
    store->count++;
    return KV_OK;
}

void *kv_get(const kv_store *store, const char *key) {
    for (kv_entry *e = store->buckets[hash(key, store->size)]; e; e = e->next) {
        if (strcmp(e->key, key) == 0) {
            return e->value;
        }
    }
    return NULL;
}

int kv_each(kv_store *store, kv_visit_fn visit, void *ctx) {
    int (*visitor)(const kv_entry *, void *) = visit ? visit : pick_visitor(kv_debug);
    for (size_t i = 0; i < store->size; i++) {
        for (kv_entry *e = store->buckets[i]; e; e = e->next) {
            (*visitor)(e, ctx);
        }
    }
    return KV_MIN(store->count, 100);
}

static size_t hash(const char *key, size_t size) {
    size_t h = 5381;
    while (*key) {
        h = h * 33 + (unsigned char)*key++;
    }
    return h % size;
}

static int print_entry(const kv_entry *entry, void *ctx) {
    (void)ctx;
    return printf("%s\\n", entry->key);
}

static int (*pick_visitor(int verbose))(const kv_entry *, void *) {
    return verbose ? print_entry : NULL;
}

void kv_close(kv_store *store) {
    free(store->buckets);
    free(store);
}
"""


def _parse(path: str, source: str, kinds: list[QueryKind] | None = None) -> ParseResult:
    result = parse_file(path, source, kinds=kinds)
    assert (result.status, result.language, result.error_count) == (ParseStatus.PARSED, "c", 0)
    return result


def _matches(result: ParseResult, kind: QueryKind, *roles: str) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for match in result.captures[kind]:
        values = [result.text(n) if (n := match.role(r)) is not None else "" for r in roles]
        found.append((match.kind, *values))
    return found


def _symbols(path: str, source: str) -> dict[str, Symbol]:
    symbols = extract_symbols(_parse(path, source), "c")
    keys = [s.symbol_key for s in symbols]
    assert len(keys) == len(set(keys))
    assert keys[0] == f"c {path}/"
    return {s.symbol_key.removeprefix(f"c {path}/"): s for s in symbols}


@pytest.fixture(scope="module")
def header() -> dict[str, Symbol]:
    return _symbols("include/kv/store.h", HEADER)


@pytest.fixture(scope="module")
def store() -> dict[str, Symbol]:
    return _symbols("src/store.c", SOURCE)


# ---------------------------------------------------------------------------- queries


def test_definitions_one_per_declarator_with_typedefs_and_macros() -> None:
    result = _parse("include/kv/store.h", HEADER, [QueryKind.DEFS])
    # A declarator's name isn't captured (the symbols find it): the declarator is.
    found = [
        (m.kind, result.text(m.role("name") or m.node)) for m in result.captures[QueryKind.DEFS]
    ]
    assert found == [
        ("definition.macro_value", "KV_MAX_KEY"),
        ("definition.macro_value", "KV_VERSION"),
        ("definition.macro", "KV_MIN"),
        ("definition.enum", "kv_status"),
        ("definition.field", "KV_OK"),
        ("definition.field", "KV_MISSING"),
        ("definition.field", "KV_FULL"),
        ("definition.type", "kv_status"),
        ("definition.struct", "kv_entry"),
        ("definition.field", "key[KV_MAX_KEY]"),
        ("definition.field", "*value"),
        ("definition.field", "*next"),
        ("definition.type", "kv_entry"),
        ("definition.struct", "kv_ops"),
        ("definition.field", "(*open)(const char *path)"),
        ("definition.field", "(*close)(void)"),
        ("definition.type", "kv_store"),
        ("definition.type", "(*kv_visit_fn)(const kv_entry *entry, void *ctx)"),
        ("definition.union", "kv_value"),
        ("definition.field", "number"),
        ("definition.field", "*text"),
        ("definition.variable", "kv_debug"),
        ("definition.variable", "*kv_open(size_t buckets)"),
        ("definition.variable", "kv_put(kv_store *store, const char *key, void *value)"),
        ("definition.variable", "*kv_get(const kv_store *store, const char *key)"),
        ("definition.variable", "kv_each(kv_store *store, kv_visit_fn visit, void *ctx)"),
        ("definition.variable", "kv_close(kv_store *store)"),
    ]


def test_includes_are_local_or_system() -> None:
    header = _parse("include/kv/store.h", HEADER, [QueryKind.IMPORTS])
    assert _matches(header, QueryKind.IMPORTS, "source") == [
        ("import.system", "<stddef.h>"),
        ("import.local", "kv/config.h"),
    ]
    source = _parse("src/store.c", SOURCE, [QueryKind.IMPORTS])
    assert _matches(source, QueryKind.IMPORTS, "source") == [
        ("import.local", "kv/store.h"),
        ("import.system", "<stdlib.h>"),
        ("import.system", "<string.h>"),
    ]
    # An include of a macro names no file.
    macro = parse_file("a.c", "#include CONFIG_H\n", kinds=[QueryKind.IMPORTS])
    assert macro.captures[QueryKind.IMPORTS] == ()
    # An include binds no name.
    assert imported_names(_parse("src/store.c", SOURCE)) == []


def test_calls_plain_through_function_pointers_and_macros() -> None:
    result = _parse("src/store.c", SOURCE, [QueryKind.CALLS])
    found = _matches(result, QueryKind.CALLS, "receiver", "name", "arguments")
    assert found[:3] == [
        ("call", "", "calloc", "(1, sizeof *store)"),
        ("call", "", "calloc", "(buckets, sizeof(kv_entry *))"),
        ("call", "", "hash", "(key, store->size)"),
    ]
    # Through a struct's function pointer: the struct (or a pointer to it) is the receiver.
    assert ("call.method", "store->ops", "open", '("kv.log")') in found
    assert ("call", "", "visitor", "(e, ctx)") in found  # (*visitor)(e, ctx)
    assert ("call", "", "KV_MIN", "(store->count, 100)") in found  # a macro, called alike
    dotted = parse_file("a.c", "void f(void) { s.cb(1); }\n", kinds=[QueryKind.CALLS])
    assert _matches(dotted, QueryKind.CALLS, "receiver", "name") == [("call.method", "s", "cb")]


def test_strings_each_part_on_its_own() -> None:
    result = _parse("src/store.c", SOURCE, [QueryKind.STRINGS])
    texts = [result.text(m.node) for m in result.captures[QueryKind.STRINGS]]
    assert texts[:4] == ['"kv/store.h"', '"ok"', '"missing"', '"full"']
    joined = parse_file("a.c", 'const char *q = "GET " "/orders";\n', kinds=[QueryKind.STRINGS])
    assert [joined.text(m.node) for m in joined.captures[QueryKind.STRINGS]] == [
        '"GET "',
        '"/orders"',
    ]


def test_references_tell_function_pointers_fields_and_types_apart() -> None:
    result = parse_file("a.c", "int f(struct dev *d) { return d->ops->open(d, MODE); }\n")
    assert [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.REFS]] == [
        ("reference.identifier", "f"),
        ("reference.type", "dev"),
        ("reference.identifier", "d"),
        ("reference.identifier", "d"),
        ("reference.field", "ops"),
        ("reference.method", "open"),
        ("reference.identifier", "d"),
        ("reference.identifier", "MODE"),
    ]


# ---------------------------------------------------------------------------- symbols


def test_keys_kinds_and_subkinds(header: dict[str, Symbol]) -> None:
    found = {key: (s.kind, s.attrs.get("subkind")) for key, s in header.items()}
    assert found == {
        "": (SymbolKind.MODULE, None),
        "KV_MAX_KEY.": (SymbolKind.VARIABLE, "macro"),
        "KV_VERSION.": (SymbolKind.VARIABLE, "macro"),
        "KV_MIN().": (SymbolKind.FUNCTION, "macro"),
        "kv_status#": (SymbolKind.CLASS, "enum"),
        "kv_status#KV_OK.": (SymbolKind.VARIABLE, "field"),
        "kv_status#KV_MISSING.": (SymbolKind.VARIABLE, "field"),
        "kv_status#KV_FULL.": (SymbolKind.VARIABLE, "field"),
        "kv_entry#": (SymbolKind.CLASS, "struct"),
        "kv_entry#key.": (SymbolKind.VARIABLE, "field"),
        "kv_entry#value.": (SymbolKind.VARIABLE, "field"),
        "kv_entry#next.": (SymbolKind.VARIABLE, "field"),
        "kv_ops#": (SymbolKind.CLASS, "struct"),
        "kv_ops#open.": (SymbolKind.VARIABLE, "field"),  # a pointer to a function: a value
        "kv_ops#close.": (SymbolKind.VARIABLE, "field"),
        "kv_store#": (SymbolKind.CLASS, "type"),  # an opaque handle
        "kv_visit_fn#": (SymbolKind.CLASS, "type"),
        "kv_value#": (SymbolKind.CLASS, "union"),
        "kv_value#number.": (SymbolKind.VARIABLE, "field"),
        "kv_value#text.": (SymbolKind.VARIABLE, "field"),
        "kv_debug.": (SymbolKind.VARIABLE, None),
        # Prototypes: functions declared, not defined here.
        "kv_open().": (SymbolKind.FUNCTION, "signature"),
        "kv_put().": (SymbolKind.FUNCTION, "signature"),
        "kv_get().": (SymbolKind.FUNCTION, "signature"),
        "kv_each().": (SymbolKind.FUNCTION, "signature"),
        "kv_close().": (SymbolKind.FUNCTION, "signature"),
    }


def test_names_are_found_through_pointer_and_parenthesized_declarators(
    store: dict[str, Symbol],
) -> None:
    assert store["kv_open()."].kind is SymbolKind.FUNCTION  # kv_store *kv_open(...)
    # A function returning a pointer to a function: the name is the innermost one's.
    assert store["pick_visitor()."].signature == (
        "static int (*pick_visitor(int verbose))(const kv_entry *, void *)"
    )
    shapes = _symbols(
        "src/shapes.c",
        "int (area)(int w, int h) { return w * h; }\n"
        "int **grid(int n);\n"
        "int (*hook)(void);\n"
        "char *const names[2];\n",
    )
    assert {key: s.kind for key, s in shapes.items()} == {
        "": SymbolKind.MODULE,
        "area().": SymbolKind.FUNCTION,
        "grid().": SymbolKind.FUNCTION,
        "hook.": SymbolKind.VARIABLE,  # a pointer to a function
        "names.": SymbolKind.VARIABLE,
    }


def test_a_prototype_and_its_definition_in_one_file_are_one_symbol(
    store: dict[str, Symbol],
) -> None:
    # static size_t hash(...); above, its definition below: one symbol, the definition.
    hashed = store["hash()."]
    assert (hashed.start_line, hashed.end_line, hashed.attrs) == (59, 65, {})
    assert "hash().~1" not in store
    assert "subkind" not in store["pick_visitor()."].attrs
    # Its parameters' names may differ; their types are the same.
    renamed = _symbols("a.c", "int add(int, int);\nint add(int a, int b) { return a + b; }\n")
    assert list(renamed) == ["", "add()."]
    assert renamed["add()."].start_line == 2


def test_a_header_prototype_and_its_source_definition_are_a_symbol_each(
    header: dict[str, Symbol], store: dict[str, Symbol]
) -> None:
    declared, defined = header["kv_put()."], store["kv_put()."]
    assert declared.symbol_key == "c include/kv/store.h/kv_put()."
    assert defined.symbol_key == "c src/store.c/kv_put()."
    assert (declared.attrs, defined.attrs) == ({"subkind": "signature"}, {})
    assert declared.signature == defined.signature


def test_typedefs_name_anonymous_types_and_a_repeated_tag_is_one_type(
    header: dict[str, Symbol],
) -> None:
    # typedef enum { ... } kv_status;: the enum kv_status, its constants under it.
    assert header["kv_status#KV_OK."].container_key == "c include/kv/store.h/kv_status#"
    # typedef struct kv_entry { ... } kv_entry;: one type, the struct.
    assert [k for k in header if k.startswith("kv_entry#")][:1] == ["kv_entry#"]
    assert "kv_entry#~1" not in header
    several = _symbols("a.c", "typedef struct { int x; } point, *point_ref;\n")
    assert {key: s.attrs.get("subkind") for key, s in several.items()} == {
        "": None,
        "point#": "struct",
        "point#x.": "field",
        "point_ref#": "type",
    }


def test_a_functions_locals_are_not_symbols(store: dict[str, Symbol]) -> None:
    # kv_each's int (*visitor)(...) and its loops' variables, kv_open's store.
    nested = [key for key in store if key.startswith(("kv_each().", "kv_open()."))]
    assert nested == ["kv_open().", "kv_each()."]
    assert not any(key.startswith(("visitor", "store.", "e.")) for key in store)
    nested = _symbols("a.c", "void f(void) { typedef int n_t; int g(void); static int n; }\n")
    assert list(nested) == ["", "f()."]


def test_macros_with_a_value_are_constants_an_include_guard_is_none(
    header: dict[str, Symbol],
) -> None:
    assert "KV_STORE_H." not in header
    assert header["KV_MAX_KEY."].signature == "#define KV_MAX_KEY 64"
    assert header["KV_VERSION."].signature == '#define KV_VERSION "1.2.0"'
    assert header["KV_MIN()."].signature == "#define KV_MIN(a, b)"


def test_an_extern_declaration_and_its_definition_in_one_file_are_one_symbol(
    header: dict[str, Symbol], store: dict[str, Symbol]
) -> None:
    assert header["kv_debug."].signature == "extern int kv_debug"  # declared only, in its header
    assert store["kv_debug."].signature == "int kv_debug = 0"
    both = _symbols("a.c", "extern int ready;\nint ready = 1;\n")
    assert list(both) == ["", "ready."]
    assert both["ready."].start_line == 2


def test_spans_include_a_typedef_and_one_of_several_declarators_spans_itself(
    header: dict[str, Symbol], store: dict[str, Symbol]
) -> None:
    spans = {key: (s.start_line, s.end_line) for key, s in header.items()}
    assert spans["kv_status#"] == (13, 17)  # typedef enum { ... } kv_status;
    assert spans["kv_entry#"] == (20, 24)
    assert spans["kv_entry#next."] == (23, 23)
    assert spans["kv_open()."] == (43, 43)
    assert spans[""] == (1, 49)
    count, size = store["kv_store#count."], store["kv_store#size."]  # size_t count, size;
    assert (count.start_line, count.start_col, count.end_col) == (8, 11, 16)
    assert (size.start_col, size.end_col) == (18, 22)
    assert store["kv_put()."].start_line == 26  # its comment is its docstring, not its span


def test_signatures_stop_at_the_body(header: dict[str, Symbol], store: dict[str, Symbol]) -> None:
    assert header["kv_status#"].signature == "typedef enum {...} kv_status"
    assert header["kv_entry#"].signature == "typedef struct kv_entry {...} kv_entry"
    assert header["kv_entry#key."].signature == "char key[KV_MAX_KEY]"
    assert header["kv_ops#"].signature == "struct kv_ops"
    assert header["kv_ops#open."].signature == "int (*open)(const char *path)"
    assert header["kv_visit_fn#"].signature == (
        "typedef int (*kv_visit_fn)(const kv_entry *entry, void *ctx)"
    )
    assert header["kv_status#KV_OK."].signature == "KV_OK = 0"
    assert header["kv_get()."].signature == "void *kv_get(const kv_store *store, const char *key)"
    assert store["kv_open()."].signature == "kv_store *kv_open(size_t buckets)"
    assert store["kv_names."].signature == (
        'static const char *const kv_names[] = {"ok", "missing", "full"}'
    )
    assert store["kv_store#size."].signature == "size_t size"  # its declaration's type


def test_block_and_line_comments_above_are_docstrings(
    header: dict[str, Symbol], store: dict[str, Symbol]
) -> None:
    assert header["kv_status#"].docstring == "What a lookup can come back with."
    assert header["kv_entry#"].docstring == "One entry of a bucket's chain."
    assert header["kv_ops#"].docstring == "The operations a backend provides."
    assert header["kv_open()."].docstring == "Make an empty store with room for n buckets."
    assert store["kv_put()."].docstring == "Insert or replace the value under key."
    assert header[""].docstring is None  # the licence above the guard documents nothing
    assert store["kv_get()."].docstring is None
    assert all("bases" not in s.attrs and "decorators" not in s.attrs for s in header.values())


def test_an_extern_c_block_split_by_ifdef_still_gives_its_symbols() -> None:
    source = (
        "#ifdef __cplusplus\n"
        'extern "C" {\n'
        "#endif\n"
        "int kv_version(void);\n"
        "#ifdef __cplusplus\n"
        "}\n"
        "#endif\n"
    )
    result = parse_file("include/kv/version.h", source)
    assert (result.status, result.language) == (ParseStatus.PARSED, "c")
    keys = [s.symbol_key for s in extract_symbols(result, "c")]
    assert keys == ["c include/kv/version.h/", "c include/kv/version.h/kv_version()."]


# ---------------------------------------------------------------------------- end to end


def test_a_c_file_is_parsed_keyed_and_chunked_by_definition() -> None:
    path = "src/store.c"
    steps = "".join(f"    h = h * {31 + i} + (unsigned char)key[{i}];\n" for i in range(30))
    source = SOURCE.replace("    size_t h = 5381;\n", "    size_t h = 5381;\n" + steps)
    result = parse_file(path, source.encode())  # the language comes from the extension
    assert (result.status, result.language) == (ParseStatus.PARSED, "c")
    assert all(result.captures[k] for k in (QueryKind.DEFS, QueryKind.IMPORTS, QueryKind.CALLS))
    chunks = chunk_file(path, "c", source)
    keyed = [(c.symbol_key or "").removeprefix(f"c {path}/") for c in chunks]
    # The struct, a C type, holds no code: it merges with its neighbours; the long function is a
    # chunk of its own.
    assert "hash()." in keyed
    assert "kv_store#" not in keyed
    long = chunks[keyed.index("hash().")]
    assert long.kind.value == "function"
    assert "lang: c" in long.header
    assert long.content.startswith("static size_t hash(const char *key, size_t size) {")


# ---------------------------------------------------------------------------- lanes


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_store.c", "test"),
        ("src/test_store.c", "test"),
        ("src/store_test.c", "test"),
        ("src/store.c", "service"),
        ("include/kv/store.h", "service"),
        ("src/testing.c", "service"),
    ],
)
def test_c_test_files_are_tests(path: str, expected: str) -> None:
    assert assign_role(path, "c", "int x;\n", ui_package=False).value == expected


def test_a_comment_of_long_runs_of_spaces_and_deep_nesting_cost_little() -> None:
    """A docstring is read in linear time (a pattern for the comment's end backtracked on runs
    of spaces), and a thousand nested structs key without recursing a thousand deep."""
    spaced = b"/* a" + b" " * 150_000 + b"b */\nint f(void) { return 0; }\n"
    started = time.monotonic()
    [f] = [s for s in extract_symbols(parse_file("spaced.c", spaced), "c") if s.name == "f"]
    assert time.monotonic() - started < 2
    assert f.docstring is not None
    assert f.docstring.startswith("a")
    deep = "".join(f"struct s{i} {{ " for i in range(1200)) + "int x; " + "};" * 1200 + "\n"
    symbols = extract_symbols(parse_file("deep.c", deep.encode()), "c")
    assert len([s for s in symbols if s.kind is SymbolKind.CLASS]) == 1200
