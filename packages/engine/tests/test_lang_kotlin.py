"""Kotlin: the queries (definitions, imports, calls, annotations, strings, references) and the
symbols of a small Spring Boot service and a Ktor module (a controller, an entity, data classes,
an enum, objects and companions), keyed, spanned and described; overloads, nested types, the
lanes of Spring's packages and a file going through parsing, symbols and chunks end to end."""

from __future__ import annotations

import pytest

from sensei_engine.chunk.code import imported_names
from sensei_engine.chunk.files import chunk_file
from sensei_engine.graph.snapshot import lane
from sensei_engine.ingest.roles import assign_role
from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult, ParseStatus, decorated, parse_file
from sensei_engine.symbols.extract import Symbol, SymbolKind, extract_symbols

ROOT = "src/main/kotlin/com/acme/shop"

CONTROLLER = """package com.acme.shop.web

import com.acme.shop.service.OrderService
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.*
import java.time.Instant as Clock

/**
 * Orders, over HTTP.
 */
@RestController
@RequestMapping("/api/orders")
class OrderController(private val orderService: OrderService) : BaseController(), Audited {

    var served: Int = 0
        private set

    /** One order. */
    @GetMapping("/{id}")
    fun getOrder(@PathVariable id: Long): OrderDto = orderService.find(id).toDto()

    // Places an order.
    @PostMapping
    suspend fun create(@RequestBody body: OrderDto): ResponseEntity<OrderDto> {
        val order = Order(body.customerId, Status.NEW)
        orderService.place(order)
        return ResponseEntity.ok(order.toDto())
    }

    fun create(customerId: Long) = create(OrderDto(customerId, total = 0))

    constructor(service: OrderService, clock: Clock) : this(service)

    companion object {
        const val PAGE_SIZE = 50
        fun of(service: OrderService): OrderController = OrderController(service)
    }

    inner class Paging {
        fun next(page: Int) = page + 1
    }
}
"""

ENTITY = """package com.acme.shop.domain

/*
 * Copyright Acme. Not a description of the class below.
 */
@Entity
@Table(name = "orders")
data class Order(
    @Id @GeneratedValue val id: Long? = null,
    @Column(name = "customer_id") val customerId: Long,
    var status: Status = Status.NEW,
    note: String = "",
) {
    fun isOpen() = status != Status.CLOSED
}

/** Where an order is. */
enum class Status(val code: String) {
    NEW("n"),
    PAID("p"),
    CLOSED("c");

    fun next(): Status = values()[ordinal + 1]
}

interface Audited : Comparable<Audited> {
    fun audit(): String
}

object OrderNumbers : Generator by SequenceGenerator("ord") {
    val prefix = "ORD-"
}

sealed class Result<out T>

typealias OrderId = Long

const val MAX_LINES = 100
"""

KTOR = """package com.acme.shop

import io.ktor.server.application.*
import io.ktor.server.routing.*

fun Application.orderRoutes(service: OrderService) {
    routing {
        get("/orders/{id}") {
            val id = call.parameters["id"]!!.toLong()
            call.respond(service.find(id))
        }
        post("/orders") {
            call.respond(service.place(call.receive<OrderDto>()))
        }
    }
}

fun String.slug(): String = lowercase().replace(' ', '-')
"""


def _parse(name: str, source: str, kinds: list[QueryKind] | None = None) -> ParseResult:
    result = parse_file(f"{ROOT}/{name}", source, kinds=kinds)
    expected = (ParseStatus.PARSED, "kotlin", 0)
    assert (result.status, result.language, result.error_count) == expected
    return result


def _matches(result: ParseResult, kind: QueryKind, *roles: str) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for match in result.captures[kind]:
        values = [result.text(n) if (n := match.role(r)) is not None else "" for r in roles]
        found.append((match.kind, *values))
    return found


def _symbols(name: str, source: str) -> dict[str, Symbol]:
    symbols = extract_symbols(_parse(name, source), "kotlin")
    keys = [s.symbol_key for s in symbols]
    assert len(keys) == len(set(keys))
    prefix = f"kt {ROOT}/{name}/"
    assert keys[0] == prefix
    return {s.symbol_key.removeprefix(prefix): s for s in symbols}


@pytest.fixture(scope="module")
def controller() -> dict[str, Symbol]:
    return _symbols("web/OrderController.kt", CONTROLLER)


@pytest.fixture(scope="module")
def entity() -> dict[str, Symbol]:
    return _symbols("domain/Order.kt", ENTITY)


# ---------------------------------------------------------------------------- queries


def test_definitions_of_every_kind_of_type_and_member() -> None:
    result = _parse("domain/Order.kt", ENTITY, [QueryKind.DEFS])
    assert _matches(result, QueryKind.DEFS, "name") == [
        ("definition.data_class", "Order"),
        ("definition.field", "id"),
        ("definition.field", "customerId"),
        ("definition.field", "status"),  # note (no val or var) is only a parameter
        ("definition.method", "isOpen"),
        ("definition.enum", "Status"),
        ("definition.field", "code"),
        ("definition.field", "NEW"),
        ("definition.field", "PAID"),
        ("definition.field", "CLOSED"),
        ("definition.method", "next"),
        ("definition.interface", "Audited"),
        ("definition.method", "audit"),
        ("definition.object", "OrderNumbers"),
        ("definition.field", "prefix"),
        ("definition.class", "Result"),
        ("definition.type", "OrderId"),
        ("definition.variable", "MAX_LINES"),
    ]


def test_imports_aliases_on_demand_imports_and_the_package() -> None:
    result = _parse("web/OrderController.kt", CONTROLLER, [QueryKind.IMPORTS])
    assert _matches(result, QueryKind.IMPORTS, "path", "source", "name", "alias") == [
        ("import.package", "", "", "com.acme.shop.web", ""),
        ("import", "com.acme.shop.service.OrderService", "", "OrderService", ""),
        ("import", "org.springframework.http.ResponseEntity", "", "ResponseEntity", ""),
        ("import.wildcard", "", "org.springframework.web.bind.annotation", "*", ""),
        ("import", "java.time.Instant", "", "Instant", "Clock"),
    ]
    assert imported_names(result) == ["OrderService", "ResponseEntity", "Clock"]


def test_calls_with_receivers_trailing_lambdas_delegation_and_references() -> None:
    result = _parse("web/OrderController.kt", CONTROLLER, [QueryKind.CALLS])
    found = _matches(result, QueryKind.CALLS, "receiver", "name", "arguments")
    assert found[:3] == [
        ("call.constructor", "", "BaseController", "()"),
        ("call.method", "orderService.find(id)", "toDto", "()"),
        ("call.method", "orderService", "find", "(id)"),
    ]
    assert ("call", "", "Order", "(body.customerId, Status.NEW)") in found  # no new in Kotlin
    assert ("call.method", "ResponseEntity", "ok", "(order.toDto())") in found
    assert ("call.constructor", "", "this", "(service)") in found
    ktor = _parse("Routes.kt", KTOR, [QueryKind.CALLS])
    calls = _matches(ktor, QueryKind.CALLS, "receiver", "name")
    assert calls[:3] == [
        ("call", "", "routing"),
        ("call", "", "get"),
        ("call.method", 'call.parameters["id"]!!', "toLong"),
    ]
    assert ("call.method", "call", "respond") in calls
    references = parse_file(
        "A.kt",
        "fun f() {\n    xs.map(::toDto)\n    val g = Order::from\n}\n",
        kinds=[QueryKind.CALLS],
    )
    assert _matches(references, QueryKind.CALLS, "receiver", "name") == [
        ("call.method", "xs", "map"),
        ("call.reference", "", "toDto"),
        ("call.reference", "Order", "from"),
    ]


def test_annotations_and_what_they_annotate() -> None:
    result = _parse("web/OrderController.kt", CONTROLLER, [QueryKind.DECORATORS])
    found: list[tuple[str, str, str]] = []
    for match in result.captures[QueryKind.DECORATORS]:
        target = decorated(match.node)
        assert target is not None
        arguments = match.role("arguments")
        found.append(
            (
                result.text(match.roles["callee"][0]),
                result.text(arguments) if arguments is not None else "",
                target.type,
            )
        )
    assert found == [
        ("RestController", "", "class_declaration"),
        ("RequestMapping", '("/api/orders")', "class_declaration"),
        ("GetMapping", '("/{id}")', "function_declaration"),
        ("PathVariable", "", "parameter"),
        ("PostMapping", "", "function_declaration"),
        ("RequestBody", "", "parameter"),
    ]


def test_strings_and_raw_strings() -> None:
    result = parse_file(
        "A.kt", 'val q = """\n    select 1\n"""\nval p = "/orders"\n', kinds=[QueryKind.STRINGS]
    )
    assert [m.kind for m in result.captures[QueryKind.STRINGS]] == ["string.raw", "string"]


def test_references_tell_methods_fields_and_types_apart() -> None:
    result = parse_file("A.kt", "fun f(): X = repo.find(order.id)\n", kinds=[QueryKind.REFS])
    assert [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.REFS]] == [
        ("reference.identifier", "f"),
        ("reference.type", "X"),
        ("reference.identifier", "repo"),
        ("reference.method", "find"),
        ("reference.identifier", "order"),
        ("reference.field", "id"),
    ]


# ---------------------------------------------------------------------------- symbols


def test_keys_kinds_subkinds_and_nesting(controller: dict[str, Symbol]) -> None:
    found = {key: (s.kind, s.attrs.get("subkind")) for key, s in controller.items()}
    assert found == {
        "": (SymbolKind.MODULE, None),
        "OrderController#": (SymbolKind.CLASS, None),
        "OrderController#orderService.": (SymbolKind.VARIABLE, "field"),
        "OrderController#served.": (SymbolKind.VARIABLE, "field"),
        "OrderController#getOrder().": (SymbolKind.METHOD, None),
        "OrderController#create().": (SymbolKind.METHOD, None),
        "OrderController#create().~1": (SymbolKind.METHOD, None),
        "OrderController#OrderController().": (SymbolKind.METHOD, "constructor"),
        "OrderController#Companion#": (SymbolKind.CLASS, "companion"),
        "OrderController#Companion#PAGE_SIZE.": (SymbolKind.VARIABLE, "field"),
        "OrderController#Companion#of().": (SymbolKind.METHOD, None),
        "OrderController#Paging#": (SymbolKind.CLASS, None),
        "OrderController#Paging#next().": (SymbolKind.METHOD, None),
    }
    prefix = f"kt {ROOT}/web/OrderController.kt/"
    assert controller["OrderController#Paging#next()."].container_key == (
        f"{prefix}OrderController#Paging#"
    )
    assert controller["OrderController#Companion#of()."].qualified_name == (
        "OrderController.Companion.of"
    )
    named = _symbols(
        "A.kt", "class A {\n    companion object Factory {\n        fun x() = 1\n    }\n}\n"
    )
    assert list(named) == ["", "A#", "A#Factory#", "A#Factory#x()."]


def test_top_level_functions_and_extensions_are_functions() -> None:
    ktor = _symbols("Routes.kt", KTOR)
    assert {k: s.kind for k, s in ktor.items()} == {
        "": SymbolKind.MODULE,
        "orderRoutes().": SymbolKind.FUNCTION,
        "slug().": SymbolKind.FUNCTION,
    }
    assert ktor["orderRoutes()."].signature == (
        "fun Application.orderRoutes(service: OrderService)"
    )
    assert ktor["slug()."].signature == "fun String.slug(): String"


def test_spans_include_annotations_not_the_kdoc(controller: dict[str, Symbol]) -> None:
    spans = {key: (s.start_line, s.end_line) for key, s in controller.items()}
    assert spans["OrderController#"] == (11, 42)
    assert spans["OrderController#served."] == (15, 16)  # its accessor too
    assert spans["OrderController#getOrder()."] == (19, 20)
    assert spans["OrderController#create()."] == (23, 28)
    parameter = controller["OrderController#orderService."]
    assert (parameter.start_line, parameter.start_col, parameter.end_col) == (13, 22, 60)


def test_signatures_stop_at_the_body_without_annotations(
    controller: dict[str, Symbol], entity: dict[str, Symbol]
) -> None:
    assert controller["OrderController#"].signature == (
        "class OrderController(private val orderService: OrderService) : BaseController(), Audited"
    )
    assert controller["OrderController#getOrder()."].signature == (
        "fun getOrder(@PathVariable id: Long): OrderDto"
    )
    assert controller["OrderController#create()."].signature == (
        "suspend fun create(@RequestBody body: OrderDto): ResponseEntity<OrderDto>"
    )
    assert controller["OrderController#create().~1"].signature == "fun create(customerId: Long)"
    assert controller["OrderController#OrderController()."].signature == (
        "constructor(service: OrderService, clock: Clock) : this(service)"
    )
    assert controller["OrderController#served."].signature == "var served: Int = 0"
    assert controller["OrderController#Companion#"].signature == "companion object"
    assert entity["Order#id."].signature == "val id: Long? = null"
    assert entity["Status#PAID."].signature == 'PAID("p")'
    assert entity["OrderId#"].signature == "typealias OrderId = Long"
    assert entity["MAX_LINES."].signature == "const val MAX_LINES = 100"


def test_kdoc_and_line_comments_are_docstrings_a_plain_block_comment_isnt(
    controller: dict[str, Symbol], entity: dict[str, Symbol]
) -> None:
    assert controller["OrderController#"].docstring == "Orders, over HTTP."
    assert controller["OrderController#getOrder()."].docstring == "One order."
    assert controller["OrderController#create()."].docstring == "Places an order."
    assert controller["OrderController#create().~1"].docstring is None
    assert entity["Order#"].docstring is None  # the licence above it
    assert entity["Status#"].docstring == "Where an order is."


def test_annotations_are_decorators_and_suspend_is_async(
    controller: dict[str, Symbol], entity: dict[str, Symbol]
) -> None:
    assert controller["OrderController#"].attrs["decorators"] == [
        "@RestController",
        '@RequestMapping("/api/orders")',
    ]
    create = controller["OrderController#create()."]
    assert create.attrs == {"decorators": ["@PostMapping"], "async": True}
    assert entity["Order#"].attrs["decorators"] == ["@Entity", '@Table(name = "orders")']
    assert entity["Order#id."].attrs["decorators"] == ["@Id", "@GeneratedValue"]
    assert entity["Order#customerId."].attrs["decorators"] == ['@Column(name = "customer_id")']


def test_delegation_specifiers_are_bases(
    controller: dict[str, Symbol], entity: dict[str, Symbol]
) -> None:
    assert controller["OrderController#"].attrs["bases"] == ["BaseController", "Audited"]
    assert entity["Audited#"].attrs == {"subkind": "interface", "bases": ["Comparable<Audited>"]}
    assert entity["OrderNumbers#"].attrs == {"subkind": "object", "bases": ["Generator"]}
    assert entity["Order#"].attrs["subkind"] == "data_class"


def test_overloads_are_numbered_in_source_order() -> None:
    before = _symbols("web/OrderController.kt", CONTROLLER)
    added = CONTROLLER.replace(
        "    constructor(",
        "    fun create(customerId: Long, note: String) = Unit\n\n    constructor(",
    )
    after = _symbols("web/OrderController.kt", added)
    assert set(after) == set(before) | {"OrderController#create().~2"}
    for key in ("OrderController#create().", "OrderController#create().~1"):
        assert after[key].content_hash == before[key].content_hash


# ---------------------------------------------------------------------------- end to end


def test_a_kotlin_file_is_parsed_keyed_and_chunked_by_definition() -> None:
    path = f"{ROOT}/web/OrderController.kt"
    steps = "".join(
        f'        audit.record("step-{i}", body.customerId, body.total, {i})\n' for i in range(14)
    )
    source = CONTROLLER.replace(
        "        orderService.place(order)\n", steps + "        orderService.place(order)\n"
    )
    result = parse_file(path, source.encode())  # the language comes from the path
    assert (result.status, result.language) == (ParseStatus.PARSED, "kotlin")
    assert all(
        result.captures[kind]
        for kind in (QueryKind.DEFS, QueryKind.IMPORTS, QueryKind.CALLS, QueryKind.DECORATORS)
    )
    symbols = extract_symbols(result, "kotlin")
    assert symbols[0].symbol_key == f"kt {path}/"
    chunks = chunk_file(path, "kotlin", source)
    keyed = [(c.symbol_key or "").removeprefix(f"kt {path}/") for c in chunks]
    assert keyed[:3] == ["", "OrderController#", "OrderController#getOrder()."]
    assert "OrderController#create()." in keyed
    create = chunks[keyed.index("OrderController#create().")]
    assert create.kind.value == "method"
    assert create.content.lstrip().startswith("// Places an order.\n    @PostMapping")
    assert "lang: kotlin" in create.header


# ---------------------------------------------------------------------------- lanes


@pytest.mark.parametrize(
    ("package", "code", "expected"),
    [
        ("web/OrderController.kt", "@RestController\nclass OrderController", "api"),
        ("controller/Paging.kt", "class Paging", "api"),
        ("service/OrderService.kt", "@Service\nclass OrderService", "svc"),
        ("domain/Order.kt", "@Entity\ndata class Order(val id: Long)", "db"),
        ("model/Status.kt", "enum class Status { NEW }", "db"),
        ("repository/OrderRepository.kt", "interface OrderRepository", "db"),
        ("Routes.kt", 'fun Application.r() { routing { get("/x") { } } }', "svc"),
    ],
)
def test_spring_packages_fall_in_their_lanes(package: str, code: str, expected: str) -> None:
    path = f"{ROOT}/{package}"
    role = assign_role(path, "kotlin", code, ui_package=False)
    assert lane("class", path, role.value) == expected
