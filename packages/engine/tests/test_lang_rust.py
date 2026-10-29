"""Rust: the queries (definitions, uses, calls, attributes, strings, references) and the symbols
of a small web service (Axum and Actix handlers, a model with its impls, a repository trait and
its Postgres implementation), keyed (an impl's functions under its type), spanned and described;
modules, the lanes of a Rust layout and a file going through parsing, symbols and chunks end to
end."""

from __future__ import annotations

import pytest

from sensei_engine.chunk.code import imported_names
from sensei_engine.chunk.files import chunk_file
from sensei_engine.graph.snapshot import lane
from sensei_engine.ingest.roles import assign_role
from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult, ParseStatus, decorated, parse_file
from sensei_engine.symbols.extract import Symbol, SymbolKind, extract_symbols

MODELS = """//! The shop's orders.
use std::fmt;

use serde::{Deserialize, Serialize};

/// An order a customer placed.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Order {
    pub id: i64,
    /// In cents.
    pub total: i64,
    pub status: Status,
}

pub enum Status {
    Open,
    Paid { at: i64 },
    Refunded(i64),
}

pub struct OrderId(pub i64);

/// Reads and writes orders.
pub trait OrderRepo: Send + Sync {
    fn find(&self, id: i64) -> Option<Order>;

    fn exists(&self, id: i64) -> bool {
        self.find(id).is_some()
    }
}

impl Order {
    pub const TABLE: &'static str = "orders";

    /// A new, open order.
    pub fn new(id: i64, total: i64) -> Self {
        Self { id, total, status: Status::Open }
    }

    pub fn is_open(&self) -> bool {
        matches!(self.status, Status::Open)
    }
}

impl fmt::Display for Order {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "order {}", self.id)
    }
}

pub mod money {
    pub const CURRENCY: &str = "EUR";

    pub fn cents(amount: f64) -> i64 {
        (amount * 100.0).round() as i64
    }
}

pub type Result<T> = std::result::Result<T, crate::Error>;
"""

ROUTES = """use axum::{
    extract::{Path, State},
    routing::get,
    Json, Router,
};
use crate::db::PgPool as Pool;
use crate::models::*;

mod health;

pub fn router(pool: Pool) -> Router {
    Router::new()
        .route("/orders/:id", get(show))
        .with_state(pool)
}

async fn show(Path(id): Path<i64>, State(pool): State<Pool>) -> Json<Order> {
    let order = sqlx::query_as::<_, Order>("select * from orders where id = $1")
        .bind(id)
        .fetch_one(&pool)
        .await
        .unwrap();
    Json(order)
}

#[get("/orders/{id}")]
async fn show_actix(path: web::Path<i64>) -> impl Responder {
    HttpResponse::Ok().json(Order::new(path.into_inner(), 0))
}

impl OrderRepo for PgRepo {
    fn find(&self, id: i64) -> Option<Order> {
        self.cache.get(&id).cloned()
    }
}
"""


def _parse(path: str, source: str, kinds: list[QueryKind] | None = None) -> ParseResult:
    result = parse_file(path, source, kinds=kinds)
    expected = (ParseStatus.PARSED, "rust", 0)
    assert (result.status, result.language, result.error_count) == expected
    return result


def _matches(result: ParseResult, kind: QueryKind, *roles: str) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for match in result.captures[kind]:
        values = [result.text(n) if (n := match.role(r)) is not None else "" for r in roles]
        found.append((match.kind, *values))
    return found


def _symbols(path: str, source: str) -> dict[str, Symbol]:
    symbols = extract_symbols(_parse(path, source), "rust")
    keys = [s.symbol_key for s in symbols]
    assert len(keys) == len(set(keys))
    assert keys[0] == f"rs {path}/"
    return {s.symbol_key.removeprefix(f"rs {path}/"): s for s in symbols}


@pytest.fixture(scope="module")
def models() -> dict[str, Symbol]:
    return _symbols("src/models/order.rs", MODELS)


@pytest.fixture(scope="module")
def routes() -> dict[str, Symbol]:
    return _symbols("src/routes/orders.rs", ROUTES)


# ---------------------------------------------------------------------------- queries


def test_definitions_with_impls_traits_and_modules() -> None:
    result = _parse("src/models/order.rs", MODELS, [QueryKind.DEFS])
    assert _matches(result, QueryKind.DEFS, "owner", "name") == [
        ("definition.struct", "", "Order"),
        ("definition.field", "", "id"),
        ("definition.field", "", "total"),
        ("definition.field", "", "status"),
        ("definition.enum", "", "Status"),
        ("definition.field", "", "Open"),
        ("definition.field", "", "Paid"),
        ("definition.field", "", "at"),
        ("definition.field", "", "Refunded"),
        ("definition.struct", "", "OrderId"),
        ("definition.trait", "", "OrderRepo"),
        ("definition.method", "", "find"),
        ("definition.method", "", "exists"),
        ("definition.field", "Order", "TABLE"),
        ("definition.method", "Order", "new"),
        ("definition.method", "Order", "is_open"),
        ("definition.method", "Order", "fmt"),
        ("definition.mod", "", "money"),
        ("definition.variable", "", "CURRENCY"),
        ("definition.function", "", "cents"),
        ("definition.type", "", "Result"),
    ]


def test_uses_groups_aliases_wildcards_and_mod_declarations() -> None:
    result = _parse("src/routes/orders.rs", ROUTES, [QueryKind.IMPORTS])
    assert _matches(result, QueryKind.IMPORTS, "source", "name", "alias") == [
        ("import", "axum", "Json", ""),
        ("import", "axum", "Router", ""),
        ("import", "extract", "Path", ""),  # in a nested group: the group's path
        ("import", "extract", "State", ""),
        ("import", "routing", "get", ""),
        ("import", "crate::db", "PgPool", "Pool"),
        ("import.wildcard", "crate::models", "*", ""),
        ("import.mod", "", "health", ""),
    ]
    assert imported_names(result) == ["Json", "Router", "Path", "State", "get", "Pool", "health"]


def test_calls_methods_paths_constructors_struct_literals_and_macros() -> None:
    result = _parse("src/routes/orders.rs", ROUTES, [QueryKind.CALLS])
    found = _matches(result, QueryKind.CALLS, "receiver", "name")
    assert [(kind, name) for kind, _, name in found[:4]] == [
        ("call.method", "with_state"),
        ("call.method", "route"),
        ("call.new", "Router"),  # Type::new(), the constructor by convention
        ("call", "get"),
    ]
    assert found[1] == ("call.method", "Router::new()", "route")
    chained = {name: receiver for kind, receiver, name in found if kind == "call.method"}
    assert chained["bind"].startswith("sqlx::query_as::<_, Order>(")  # a chain's receiver
    assert ("call.method", "sqlx", "query_as") in found  # without its type arguments
    assert ("call", "", "Json") in found
    assert ("call.new", "", "Order") in found
    assert ("call.method", "HttpResponse", "Ok") in found
    assert ("call.method", "self.cache.get(&id)", "cloned") in found
    models = _parse("src/models/order.rs", MODELS, [QueryKind.CALLS])
    model_calls = _matches(models, QueryKind.CALLS, "receiver", "name", "arguments")
    assert ("call.new", "", "Self", "{ id, total, status: Status::Open }") in model_calls
    assert ("call.macro", "", "matches", "(self.status, Status::Open)") in model_calls
    assert ("call.macro", "", "write", '(f, "order {}", self.id)') in model_calls
    assert ("call.method", "(amount * 100.0)", "round", "()") in model_calls


def test_attributes_and_what_they_are_on() -> None:
    models = _parse("src/models/order.rs", MODELS, [QueryKind.DECORATORS])
    found: list[tuple[str, str, str]] = []
    for match in models.captures[QueryKind.DECORATORS]:
        target = decorated(match.node)
        assert target is not None
        found.append(
            (
                models.text(match.roles["callee"][0]),
                models.text(match.roles["arguments"][0]),
                target.type,
            )
        )
    assert found == [
        ("derive", "(Debug, Clone, Serialize, Deserialize)", "struct_item"),
        ("serde", '(rename_all = "camelCase")', "struct_item"),
    ]
    routes = _parse("src/routes/orders.rs", ROUTES, [QueryKind.DECORATORS])
    assert _matches(routes, QueryKind.DECORATORS, "callee", "arguments") == [
        ("decorator", "get", '("/orders/{id}")')
    ]
    bare = parse_file("a.rs", "#[tokio::main]\nasync fn main() {}\n", kinds=[QueryKind.DECORATORS])
    assert _matches(bare, QueryKind.DECORATORS, "callee") == [("decorator", "tokio::main")]


def test_strings_and_raw_strings() -> None:
    result = parse_file(
        "a.rs", 'const A: &str = "x";\nconst Q: &str = r#"select\n1"#;\n', kinds=[QueryKind.STRINGS]
    )
    assert [m.kind for m in result.captures[QueryKind.STRINGS]] == ["string", "string.raw"]


def test_references_tell_methods_fields_and_types_apart() -> None:
    result = parse_file("a.rs", "fn f(r: Repo) { r.db.find(r.id); }\n", kinds=[QueryKind.REFS])
    assert [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.REFS]] == [
        ("reference.identifier", "f"),
        ("reference.identifier", "r"),
        ("reference.type", "Repo"),
        ("reference.identifier", "r"),
        ("reference.field", "db"),
        ("reference.method", "find"),
        ("reference.identifier", "r"),
        ("reference.field", "id"),
    ]


# ---------------------------------------------------------------------------- symbols


def test_keys_kinds_and_subkinds(models: dict[str, Symbol]) -> None:
    found = {key: (s.kind, s.attrs.get("subkind")) for key, s in models.items()}
    assert found == {
        "": (SymbolKind.MODULE, None),
        "Order#": (SymbolKind.CLASS, "struct"),
        "Order#id.": (SymbolKind.VARIABLE, "field"),
        "Order#total.": (SymbolKind.VARIABLE, "field"),
        "Order#status.": (SymbolKind.VARIABLE, "field"),
        "Status#": (SymbolKind.CLASS, "enum"),
        "Status#Open.": (SymbolKind.VARIABLE, "field"),
        "Status#Paid.": (SymbolKind.VARIABLE, "field"),
        "Status#Paid.at.": (SymbolKind.VARIABLE, "field"),
        "Status#Refunded.": (SymbolKind.VARIABLE, "field"),
        "OrderId#": (SymbolKind.CLASS, "struct"),
        "OrderRepo#": (SymbolKind.CLASS, "trait"),
        "OrderRepo#find().": (SymbolKind.METHOD, None),
        "OrderRepo#exists().": (SymbolKind.METHOD, None),
        "Order#TABLE.": (SymbolKind.VARIABLE, "field"),
        "Order#new().": (SymbolKind.METHOD, None),
        "Order#is_open().": (SymbolKind.METHOD, None),
        "Order#fmt().": (SymbolKind.METHOD, None),
        "money#": (SymbolKind.MODULE, "module"),
        "money#CURRENCY.": (SymbolKind.VARIABLE, None),
        "money#cents().": (SymbolKind.FUNCTION, None),
        "Result#": (SymbolKind.CLASS, "type"),
    }


def test_an_impls_functions_are_keyed_under_its_type(
    models: dict[str, Symbol], routes: dict[str, Symbol]
) -> None:
    path = "src/models/order.rs"
    new = models["Order#new()."]
    assert new.symbol_key == f"rs {path}/Order#new()."
    assert new.qualified_name == "Order.new"
    assert new.container_key == f"rs {path}/"  # the impl block stands outside the struct
    assert models["Order#fmt()."].qualified_name == "Order.fmt"  # impl fmt::Display for Order
    # A trait implemented for a type of another file: keyed under that type.
    assert [k for k, s in routes.items() if s.kind is SymbolKind.METHOD] == ["PgRepo#find()."]
    generic = _symbols(
        "src/page.rs", "impl<T: Clone> crate::Page<T> {\n    fn len(&self) -> usize { 0 }\n}\n"
    )
    assert list(generic) == ["", "Page#len()."]


def test_spans_include_attributes_not_the_docs(models: dict[str, Symbol]) -> None:
    spans = {key: (s.start_line, s.end_line) for key, s in models.items()}
    assert spans["Order#"] == (7, 14)  # from its first attribute
    assert spans["Order#total."] == (12, 12)
    assert spans["Order#new()."] == (37, 39)
    assert spans["money#"] == (52, 58)


def test_signatures_stop_at_the_body(models: dict[str, Symbol], routes: dict[str, Symbol]) -> None:
    assert models["Order#"].signature == "pub struct Order"
    assert models["Order#total."].signature == "pub total: i64"
    assert models["Status#Paid."].signature == "Paid {at: i64}"
    assert models["OrderRepo#"].signature == "pub trait OrderRepo: Send + Sync"
    assert models["OrderRepo#find()."].signature == "fn find(&self, id: i64) -> Option<Order>"
    assert models["Order#new()."].signature == "pub fn new(id: i64, total: i64) -> Self"
    assert models["Order#TABLE."].signature == 'pub const TABLE: &\'static str = "orders"'
    assert models["money#"].signature == "pub mod money"
    assert (
        models["Result#"].signature == "pub type Result<T> = std::result::Result<T, crate::Error>"
    )
    assert routes["show()."].signature == (
        "async fn show(Path(id): Path<i64>, State(pool): State<Pool>) -> Json<Order>"
    )
    assert routes["health#"].signature == "mod health"


def test_outer_doc_comments_are_docstrings_inner_ones_arent(models: dict[str, Symbol]) -> None:
    assert models["Order#"].docstring == "An order a customer placed."
    assert models["Order#total."].docstring == "In cents."
    assert models["OrderRepo#"].docstring == "Reads and writes orders."
    assert models["Order#new()."].docstring == "A new, open order."
    assert models["Status#"].docstring is None
    after_inner = _symbols("src/lib.rs", "//! The crate.\nfn main() {}\n")
    assert after_inner["main()."].docstring is None


def test_attributes_are_decorators_and_async_is_kept(
    models: dict[str, Symbol], routes: dict[str, Symbol]
) -> None:
    assert models["Order#"].attrs["decorators"] == [
        "#[derive(Debug, Clone, Serialize, Deserialize)]",
        '#[serde(rename_all = "camelCase")]',
    ]
    assert routes["show_actix()."].attrs == {
        "decorators": ['#[get("/orders/{id}")]'],
        "async": True,
    }
    assert routes["show()."].attrs == {"async": True}


def test_supertraits_and_implemented_traits_are_bases(models: dict[str, Symbol]) -> None:
    assert models["OrderRepo#"].attrs["bases"] == ["Send", "Sync"]
    assert models["Order#"].attrs["bases"] == ["fmt::Display"]  # impl fmt::Display for Order
    assert "bases" not in models["Status#"].attrs


def test_a_mod_rs_file_is_named_after_its_directory() -> None:
    symbols = _symbols("src/routes/mod.rs", "pub mod orders;\n")
    assert symbols[""].name == "routes"


# ---------------------------------------------------------------------------- end to end


def test_a_rust_file_is_parsed_keyed_and_chunked_by_definition() -> None:
    path = "src/models/order.rs"
    steps = "".join(
        f'        tracing::info!(step = {i}, id = self.id, total = self.total, "checked");\n'
        for i in range(14)
    )
    source = MODELS.replace(
        "        matches!(self.status, Status::Open)\n",
        steps + "        matches!(self.status, Status::Open)\n",
    )
    result = parse_file(path, source.encode())  # the language comes from the path
    assert (result.status, result.language) == (ParseStatus.PARSED, "rust")
    assert all(
        result.captures[kind]
        for kind in (QueryKind.DEFS, QueryKind.IMPORTS, QueryKind.CALLS, QueryKind.DECORATORS)
    )
    symbols = extract_symbols(result, "rust")
    assert symbols[0].symbol_key == f"rs {path}/"
    chunks = chunk_file(path, "rust", source)
    keyed = [(c.symbol_key or "").removeprefix(f"rs {path}/") for c in chunks]
    assert "OrderRepo#" in keyed  # a trait holds code: a chunk of its own
    is_open = chunks[keyed.index("Order#is_open().")]
    assert is_open.kind.value == "method"
    assert is_open.content.lstrip().startswith("pub fn is_open(&self) -> bool {")
    assert "lang: rust" in is_open.header


# ---------------------------------------------------------------------------- lanes


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/routes/orders.rs", "api"),
        ("src/handlers/orders.rs", "api"),
        ("src/models/order.rs", "db"),
        ("src/db/mod.rs", "db"),
        ("src/services/checkout.rs", "svc"),
        ("src/main.rs", "svc"),
    ],
)
def test_a_rust_layouts_folders_fall_in_their_lanes(path: str, expected: str) -> None:
    role = assign_role(path, "rust", "fn main() {}\n", ui_package=False)
    assert lane("class", path, role.value) == expected
