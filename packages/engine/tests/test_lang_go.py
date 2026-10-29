"""Go: the queries (definitions, imports, calls, strings, references) and the symbols of a small
service (a Gin handler, a Postgres store, an Echo and net/http main), keyed (methods under their
receiver's type), spanned and described; embedding, grouped declarations, the lanes of a Go
layout and a file going through parsing, symbols and chunks end to end."""

from __future__ import annotations

import pytest

from sensei_engine.chunk.code import imported_names
from sensei_engine.chunk.files import chunk_file
from sensei_engine.graph.snapshot import lane
from sensei_engine.ingest.roles import assign_role
from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult, ParseStatus, parse_file
from sensei_engine.symbols.extract import Symbol, SymbolKind, extract_symbols

HANDLERS = """// Package handlers serves the shop's HTTP API.
package handlers

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/acme/shop/internal/store"
	apperr "github.com/acme/shop/internal/errors"
	"github.com/gin-gonic/gin"
)

const defaultLimit = 20

var ErrBadID = errors.New("bad order id")

// OrderHandler answers the order routes.
type OrderHandler struct {
	store store.OrderStore
	limit int `json:"limit"`
	*BaseHandler
}

// NewOrderHandler wires a handler to its store.
func NewOrderHandler(s store.OrderStore) *OrderHandler {
	return &OrderHandler{store: s, limit: defaultLimit}
}

// Register mounts the routes on r.
func (h *OrderHandler) Register(r *gin.RouterGroup) {
	r.GET("/orders/:id", h.Get)
	r.POST("/orders", h.Create)
}

func (h *OrderHandler) Get(c *gin.Context) {
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": ErrBadID.Error()})
		return
	}
	order, err := h.store.Find(c.Request.Context(), id)
	if err != nil {
		c.JSON(apperr.Status(err), gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, order)
}

func (h OrderHandler) Create(c *gin.Context) {
	var in store.Order
	if err := c.ShouldBindJSON(&in); err != nil {
		c.AbortWithStatus(http.StatusUnprocessableEntity)
		return
	}
	c.JSON(http.StatusCreated, in)
}
"""

STORE = """package store

import (
	"context"
	"database/sql"
	"io"

	_ "github.com/lib/pq"
)

type (
	// Order is a row of the orders table.
	Order struct {
		ID         int64  `db:"id" json:"id"`
		CustomerID int64  `db:"customer_id" json:"customerId"`
		Status     Status `db:"status" json:"status"`
	}

	// Status is where an order is.
	Status string
)

const (
	StatusNew  Status = "new"
	StatusPaid Status = "paid"
)

// OrderStore reads and writes orders.
type OrderStore interface {
	io.Closer
	Find(ctx context.Context, id int64) (*Order, error)
	Save(ctx context.Context, o *Order) error
}

type PostgresStore struct {
	db *sql.DB
}

const findOrder = `
SELECT id, customer_id, status
FROM orders
WHERE id = $1`

func (s *PostgresStore) Find(ctx context.Context, id int64) (*Order, error) {
	var o Order
	row := s.db.QueryRowContext(ctx, findOrder, id)
	if err := row.Scan(&o.ID, &o.CustomerID, &o.Status); err != nil {
		return nil, err
	}
	return &o, nil
}

func (s *PostgresStore) Close() error { return s.db.Close() }

// Page is one page of results of any type.
type Page[T any] struct {
	Items []T
	Next  string
}

func (p Page[T]) Len() int { return len(p.Items) }
"""

MAIN = """package main

import (
	"log"
	"net/http"

	"github.com/labstack/echo/v4"
)

func health(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
}

func main() {
	http.HandleFunc("/health", health)
	e := echo.New()
	e.GET("/orders", func(c echo.Context) error {
		return c.JSON(http.StatusOK, []string{})
	})
	log.Fatal(e.Start(":8080"))
}
"""


def _parse(path: str, source: str, kinds: list[QueryKind] | None = None) -> ParseResult:
    result = parse_file(path, source, kinds=kinds)
    assert (result.status, result.language, result.error_count) == (ParseStatus.PARSED, "go", 0)
    return result


def _matches(result: ParseResult, kind: QueryKind, *roles: str) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for match in result.captures[kind]:
        values = [result.text(n) if (n := match.role(r)) is not None else "" for r in roles]
        found.append((match.kind, *values))
    return found


def _symbols(path: str, source: str) -> dict[str, Symbol]:
    symbols = extract_symbols(_parse(path, source), "go")
    keys = [s.symbol_key for s in symbols]
    assert len(keys) == len(set(keys))
    assert keys[0] == f"go {path}/"
    return {s.symbol_key.removeprefix(f"go {path}/"): s for s in symbols}


@pytest.fixture(scope="module")
def handlers() -> dict[str, Symbol]:
    return _symbols("internal/handlers/orders.go", HANDLERS)


@pytest.fixture(scope="module")
def store() -> dict[str, Symbol]:
    return _symbols("internal/store/orders.go", STORE)


# ---------------------------------------------------------------------------- queries


def test_definitions_with_receivers_fields_and_grouped_declarations() -> None:
    result = _parse("internal/store/orders.go", STORE, [QueryKind.DEFS])
    assert _matches(result, QueryKind.DEFS, "owner", "name") == [
        ("definition.package", "", "store"),
        ("definition.struct", "", "Order"),
        ("definition.field", "", "ID"),
        ("definition.field", "", "CustomerID"),
        ("definition.field", "", "Status"),
        ("definition.type", "", "Status"),
        ("definition.variable", "", "StatusNew"),
        ("definition.variable", "", "StatusPaid"),
        ("definition.interface", "", "OrderStore"),
        ("definition.method", "", "Find"),
        ("definition.method", "", "Save"),
        ("definition.struct", "", "PostgresStore"),
        ("definition.field", "", "db"),
        ("definition.variable", "", "findOrder"),
        ("definition.method", "PostgresStore", "Find"),
        ("definition.method", "PostgresStore", "Close"),
        ("definition.struct", "", "Page"),
        ("definition.field", "", "Items"),
        ("definition.field", "", "Next"),
        ("definition.method", "Page", "Len"),
    ]


def test_imports_aliases_blank_imports_and_the_package() -> None:
    result = _parse("internal/handlers/orders.go", HANDLERS, [QueryKind.IMPORTS])
    assert _matches(result, QueryKind.IMPORTS, "source", "alias", "name") == [
        ("import.package", "", "", "handlers"),
        ("import", "errors", "", ""),
        ("import", "net/http", "", ""),
        ("import", "strconv", "", ""),
        ("import", "github.com/acme/shop/internal/store", "", ""),
        ("import", "github.com/acme/shop/internal/errors", "apperr", ""),
        ("import", "github.com/gin-gonic/gin", "", ""),
    ]
    blank = _parse("internal/store/orders.go", STORE, [QueryKind.IMPORTS])
    assert ("import.blank", "github.com/lib/pq") in _matches(blank, QueryKind.IMPORTS, "source")
    dot = parse_file("a.go", 'package a\nimport . "fmt"\n', kinds=[QueryKind.IMPORTS])
    assert _matches(dot, QueryKind.IMPORTS, "source")[-1] == ("import.dot", "fmt")


def test_imported_names_are_aliases_and_package_names() -> None:
    assert imported_names(_parse("internal/handlers/orders.go", HANDLERS)) == [
        "errors",
        "http",
        "strconv",
        "store",
        "apperr",
        "gin",
    ]
    assert imported_names(_parse("cmd/api/main.go", MAIN)) == ["log", "http", "echo"]


def test_calls_with_receivers_bare_calls_and_composite_literals() -> None:
    result = _parse("internal/handlers/orders.go", HANDLERS, [QueryKind.CALLS])
    found = _matches(result, QueryKind.CALLS, "receiver", "name", "arguments")
    assert found[:4] == [
        ("call.method", "errors", "New", '("bad order id")'),
        ("call.new", "", "OrderHandler", "{store: s, limit: defaultLimit}"),
        ("call.method", "r", "GET", '("/orders/:id", h.Get)'),
        ("call.method", "r", "POST", '("/orders", h.Create)'),
    ]
    assert ("call.method", "h.store", "Find", "(c.Request.Context(), id)") in found
    assert ("call.method", "c.Request", "Context", "()") in found
    assert ("call.method", "strconv", "ParseInt", '(c.Param("id"), 10, 64)') in found
    store = _parse("internal/store/orders.go", STORE, [QueryKind.CALLS])
    assert ("call", "", "len", "(p.Items)") in _matches(
        store, QueryKind.CALLS, "receiver", "name", "arguments"
    )
    generic = parse_file(
        "a.go", "package a\nfunc f() { _ = models.Page[Order]{Next: x} }\n", kinds=[QueryKind.CALLS]
    )
    assert _matches(generic, QueryKind.CALLS, "name") == [("call.new", "models.Page")]


def test_strings_and_raw_strings() -> None:
    result = _parse("internal/store/orders.go", STORE, [QueryKind.STRINGS])
    raw = [
        result.text(m.node) for m in result.captures[QueryKind.STRINGS] if m.kind == "string.raw"
    ]
    assert raw[0] == '`db:"id" json:"id"`'  # a struct tag
    assert raw[-1].startswith("`\nSELECT id, customer_id, status")
    plain = [m.kind for m in result.captures[QueryKind.STRINGS] if m.kind == "string"]
    assert len(plain) == 6  # the import paths, "new" and "paid"


def test_references_tell_methods_fields_packages_and_types_apart() -> None:
    result = parse_file("a.go", "package a\nfunc f(s *Server) { s.store.Find(ctx.ID) }\n")
    assert [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.REFS]] == [
        ("reference.package", "a"),
        ("reference.identifier", "f"),
        ("reference.identifier", "s"),
        ("reference.type", "Server"),
        ("reference.identifier", "s"),
        ("reference.field", "store"),
        ("reference.method", "Find"),
        ("reference.identifier", "ctx"),
        ("reference.field", "ID"),
    ]


# ---------------------------------------------------------------------------- symbols


def test_keys_kinds_and_subkinds(store: dict[str, Symbol]) -> None:
    found = {key: (s.kind, s.attrs.get("subkind"), s.qualified_name) for key, s in store.items()}
    assert found == {
        "": (SymbolKind.MODULE, None, "internal.store.orders"),
        "store#": (SymbolKind.MODULE, "package", "store"),
        "Order#": (SymbolKind.CLASS, "struct", "Order"),
        "Order#ID.": (SymbolKind.VARIABLE, "field", "Order.ID"),
        "Order#CustomerID.": (SymbolKind.VARIABLE, "field", "Order.CustomerID"),
        "Order#Status.": (SymbolKind.VARIABLE, "field", "Order.Status"),
        "Status#": (SymbolKind.CLASS, "type", "Status"),
        "StatusNew.": (SymbolKind.VARIABLE, None, "StatusNew"),
        "StatusPaid.": (SymbolKind.VARIABLE, None, "StatusPaid"),
        "OrderStore#": (SymbolKind.CLASS, "interface", "OrderStore"),
        "OrderStore#Find().": (SymbolKind.METHOD, None, "OrderStore.Find"),
        "OrderStore#Save().": (SymbolKind.METHOD, None, "OrderStore.Save"),
        "PostgresStore#": (SymbolKind.CLASS, "struct", "PostgresStore"),
        "PostgresStore#db.": (SymbolKind.VARIABLE, "field", "PostgresStore.db"),
        "findOrder.": (SymbolKind.VARIABLE, None, "findOrder"),
        "PostgresStore#Find().": (SymbolKind.METHOD, None, "PostgresStore.Find"),
        "PostgresStore#Close().": (SymbolKind.METHOD, None, "PostgresStore.Close"),
        "Page#": (SymbolKind.CLASS, "struct", "Page"),
        "Page#Items.": (SymbolKind.VARIABLE, "field", "Page.Items"),
        "Page#Next.": (SymbolKind.VARIABLE, "field", "Page.Next"),
        "Page#Len().": (SymbolKind.METHOD, None, "Page.Len"),
    }


def test_methods_are_keyed_under_their_receivers_type(handlers: dict[str, Symbol]) -> None:
    methods = [key for key, s in handlers.items() if s.kind is SymbolKind.METHOD]
    # Pointer and value receivers alike.
    assert methods == ["OrderHandler#Register().", "OrderHandler#Get().", "OrderHandler#Create()."]
    path = "internal/handlers/orders.go"
    assert handlers["OrderHandler#Get()."].symbol_key == f"go {path}/OrderHandler#Get()."
    # They stand at the top of the file, outside the struct: the module holds them.
    assert handlers["OrderHandler#Get()."].container_key == f"go {path}/"
    assert handlers["OrderHandler#limit."].container_key == f"go {path}/OrderHandler#"
    # A receiver whose type is in another file of the package is keyed the same way.
    other = _symbols(
        "internal/handlers/health.go", "package handlers\n\nfunc (h *OrderHandler) Health() {}\n"
    )
    assert list(other) == ["", "handlers#", "OrderHandler#Health()."]


def test_spans_of_grouped_and_single_declarations(store: dict[str, Symbol]) -> None:
    spans = {key: (s.start_line, s.end_line) for key, s in store.items()}
    assert spans["Order#"] == (13, 17)  # the spec in its group, not the group
    assert spans["Status#"] == (20, 20)
    assert spans["StatusNew."] == (24, 24)
    assert spans["OrderStore#"] == (29, 33)  # a single spec spans its declaration: type too
    assert spans["findOrder."] == (39, 42)
    assert spans["PostgresStore#Close()."] == (53, 53)
    assert (store["OrderStore#"].start_col, store["Order#"].start_col) == (0, 1)


def test_signatures_stop_at_the_body(handlers: dict[str, Symbol], store: dict[str, Symbol]) -> None:
    assert handlers["OrderHandler#"].signature == "type OrderHandler struct"
    assert handlers["NewOrderHandler()."].signature == (
        "func NewOrderHandler(s store.OrderStore) *OrderHandler"
    )
    assert handlers["OrderHandler#Get()."].signature == "func (h *OrderHandler) Get(c *gin.Context)"
    assert handlers["OrderHandler#limit."].signature == 'limit int `json:"limit"`'
    assert handlers["defaultLimit."].signature == "const defaultLimit = 20"
    assert handlers["ErrBadID."].signature == 'var ErrBadID = errors.New("bad order id")'
    assert store["OrderStore#"].signature == "type OrderStore interface"
    assert store["OrderStore#Find()."].signature == (
        "Find(ctx context.Context, id int64) (*Order, error)"
    )
    assert store["Status#"].signature == "type Status string"
    assert store["StatusPaid."].signature == 'const StatusPaid Status = "paid"'
    assert store["Page#"].signature == "type Page[T any] struct"
    assert store["Page#Len()."].signature == "func (p Page[T]) Len() int"


def test_comments_above_are_docstrings(
    handlers: dict[str, Symbol], store: dict[str, Symbol]
) -> None:
    assert handlers["handlers#"].docstring == "Package handlers serves the shop's HTTP API."
    assert handlers["OrderHandler#"].docstring == "OrderHandler answers the order routes."
    assert handlers["OrderHandler#Register()."].docstring == "Register mounts the routes on r."
    assert handlers["OrderHandler#Get()."].docstring is None
    assert store["Order#"].docstring == "Order is a row of the orders table."  # in its group
    assert store["Status#"].docstring == "Status is where an order is."
    assert store["OrderStore#"].docstring == "OrderStore reads and writes orders."


def test_embedded_types_are_bases(handlers: dict[str, Symbol], store: dict[str, Symbol]) -> None:
    assert handlers["OrderHandler#"].attrs == {"subkind": "struct", "bases": ["BaseHandler"]}
    assert store["OrderStore#"].attrs == {"subkind": "interface", "bases": ["io.Closer"]}
    assert "bases" not in store["PostgresStore#"].attrs
    assert "decorators" not in handlers["OrderHandler#Get()."].attrs  # Go has none


# ---------------------------------------------------------------------------- end to end


def test_a_go_file_is_parsed_keyed_and_chunked_by_definition() -> None:
    path = "internal/handlers/orders.go"
    steps = "".join(f'\tc.Header("X-Step-{i}", strconv.Itoa(h.limit*{i}))\n' for i in range(14))
    source = HANDLERS.replace(
        "\tc.JSON(http.StatusOK, order)\n", steps + "\tc.JSON(http.StatusOK, order)\n"
    ).replace("\tc.JSON(http.StatusCreated, in)\n", steps + "\tc.JSON(http.StatusCreated, in)\n")
    result = parse_file(path, source.encode())  # the language comes from the path
    assert (result.status, result.language) == (ParseStatus.PARSED, "go")
    assert all(result.captures[k] for k in (QueryKind.DEFS, QueryKind.IMPORTS, QueryKind.CALLS))
    symbols = extract_symbols(result, "go")
    assert symbols[0].symbol_key == f"go {path}/"
    chunks = chunk_file(path, "go", source)
    keyed = [(c.symbol_key or "").removeprefix(f"go {path}/") for c in chunks]
    # The small declarations merge; the long methods are chunks of their own.
    assert keyed[-2:] == ["OrderHandler#Get().", "OrderHandler#Create()."]
    assert [c.kind.value for c in chunks][-2:] == ["method", "method"]
    assert "lang: go" in chunks[-1].header
    assert chunks[-1].content.startswith("func (h OrderHandler) Create(c *gin.Context) {")


# ---------------------------------------------------------------------------- lanes


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("internal/handlers/orders.go", "api"),
        ("internal/api/router.go", "api"),
        ("api/orders.go", "api"),
        ("internal/models/order.go", "db"),
        ("internal/store/orders.go", "db"),
        ("internal/store/postgres/orders.go", "db"),
        ("internal/repository/orders.go", "db"),
        ("internal/service/checkout.go", "svc"),
        ("cmd/worker/main.go", "svc"),
    ],
)
def test_a_go_layouts_folders_fall_in_their_lanes(path: str, expected: str) -> None:
    role = assign_role(path, "go", "package x\n", ui_package=False)
    assert lane("class", path, role.value) == expected


def test_a_go_test_file_is_a_test_wherever_it_is() -> None:
    role = assign_role("internal/handlers/orders_test.go", "go", "package x\n", ui_package=False)
    assert role.value == "test"


def test_a_frontend_store_is_not_the_data_layer() -> None:
    role = assign_role("web/src/store/orders.ts", "typescript", "", ui_package=False)
    assert role.value == "service"
