"""Java: the queries (definitions, imports, calls, annotations, strings, references) and the
symbols of a small Spring Boot application (a controller, a service, a repository, an entity, a
record), keyed, spanned and described; overloads, nested types, the lanes of Spring's packages
and a file going through parsing, symbols and chunks end to end."""

from __future__ import annotations

import pytest

from sensei_engine.chunk.files import chunk_file
from sensei_engine.graph.snapshot import lane
from sensei_engine.ingest.roles import assign_role
from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult, ParseStatus, decorated, parse_file
from sensei_engine.symbols.extract import Symbol, SymbolKind, extract_symbols

ROOT = "src/main/java/com/acme/shop"

CONTROLLER = """package com.acme.shop.controller;

import com.acme.shop.dto.OrderDto;
import com.acme.shop.service.OrderService;
import java.util.List;
import org.springframework.web.bind.annotation.*;
import static org.springframework.http.HttpStatus.CREATED;

/**
 * Orders, over HTTP.
 *
 * <p>Thin: the service does the work.
 */
@RestController
@RequestMapping("/api/orders")
@RequiredArgsConstructor
public class OrderController {

    private final OrderService orderService;

    /** Every order of a customer. */
    @GetMapping
    public List<OrderDto> getOrders(@RequestParam("customer") Long customerId) {
        return orderService.findByCustomer(customerId);
    }

    @GetMapping("/{id}")
    public ResponseEntity<OrderDto> getOrder(@PathVariable Long id) {
        return orderService.find(id).map(ResponseEntity::ok).orElseThrow();
    }

    // Places an order.
    // Answers 201.
    @PostMapping
    @ResponseStatus(CREATED)
    public OrderDto createOrder(
            @Valid @RequestBody OrderDto body) {
        return orderService.place(body);
    }
}
"""

SERVICE = """package com.acme.shop.service;

import com.acme.shop.entity.Order;
import com.acme.shop.repository.OrderRepository;

@Service
@RequiredArgsConstructor
public class OrderService {

    private static final int MAX_LINES = 50;

    private final OrderRepository orderRepository;
    private final PaymentClient paymentClient;

    public List<OrderDto> findByCustomer(Long customerId) {
        return orderRepository.findByCustomerId(customerId).stream().map(OrderDto::from).toList();
    }

    @Transactional
    public OrderDto place(OrderDto body) {
        return place(body, null);
    }

    @Transactional
    public OrderDto place(OrderDto body, String coupon) {
        Order order = new Order(body.customerId(), Order.Status.NEW);
        paymentClient.authorize(order.getId(), body.total(), coupon);
        return OrderDto.from(orderRepository.save(order));
    }
}
"""

REPOSITORY = """package com.acme.shop.repository;

import com.acme.shop.entity.Order;
import org.springframework.data.jpa.repository.JpaRepository;

public interface OrderRepository extends JpaRepository<Order, Long>, OrderSearch {

    List<Order> findByCustomerId(Long customerId);

    @Query("select o from Order o where o.status = :status")
    List<Order> findByStatus(@Param("status") Order.Status status);

    default boolean isOpen(Long id) {
        return findById(id).map(Order::isOpen).orElse(false);
    }
}
"""

ENTITY = """package com.acme.shop.entity;

/*
 * Copyright Acme. Not a description of the class below.
 */
@Entity
@Table(name = "orders")
@Getter
@NoArgsConstructor
public class Order extends BaseEntity implements Serializable, Comparable<Order> {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "customer_id", nullable = false)
    private Long customerId;

    @Enumerated(EnumType.STRING)
    @Column(length = 16)
    private Status status;

    /** Counted when the order is placed. */
    @Column private int lineCount, itemCount;

    public Order(Long customerId, Status status) {
        this(customerId, status, 0);
    }

    public Order(Long customerId, Status status, int lineCount) {
        super();
        this.customerId = customerId;
        this.status = status;
        this.lineCount = lineCount;
    }

    public boolean isOpen() {
        return status != Status.CLOSED;
    }

    @Override
    public int compareTo(Order other) {
        return id.compareTo(other.id);
    }

    /** Where an order is. */
    public enum Status {
        NEW("n"),
        PAID("p"),
        @Deprecated CLOSED("c");

        private final String code;

        Status(String code) {
            this.code = code;
        }
    }

    public static class Line {
        private String sku;

        class Note {
            String text;
        }
    }
}
"""

DTO = """package com.acme.shop.dto;

public record OrderDto(@NotNull Long customerId, BigDecimal total) implements Serializable {

    public OrderDto {
        Objects.requireNonNull(customerId);
    }

    public static OrderDto from(Order order) {
        return new OrderDto(order.getCustomerId(), BigDecimal.ZERO);
    }
}
"""

AUDITED = """package com.acme.shop.audit;

@Retention(RetentionPolicy.RUNTIME)
@Target({ElementType.METHOD, ElementType.TYPE})
public @interface Audited {
    String value() default "";
    int LEVEL = 1;
}
"""


def _parse(name: str, source: str, kinds: list[QueryKind] | None = None) -> ParseResult:
    result = parse_file(f"{ROOT}/{name}", source, kinds=kinds)
    assert (result.status, result.language, result.error_count) == (ParseStatus.PARSED, "java", 0)
    return result


def _matches(result: ParseResult, kind: QueryKind, *roles: str) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for match in result.captures[kind]:
        values = [result.text(n) if (n := match.role(r)) is not None else "" for r in roles]
        found.append((match.kind, *values))
    return found


def _symbols(name: str, source: str) -> dict[str, Symbol]:
    result = _parse(name, source)
    symbols = extract_symbols(result, "java")
    keys = [s.symbol_key for s in symbols]
    assert len(keys) == len(set(keys))
    prefix = f"java {ROOT}/{name}/"
    assert keys[0] == prefix
    return {s.symbol_key.removeprefix(prefix): s for s in symbols}


@pytest.fixture(scope="module")
def controller() -> dict[str, Symbol]:
    return _symbols("controller/OrderController.java", CONTROLLER)


@pytest.fixture(scope="module")
def entity() -> dict[str, Symbol]:
    return _symbols("entity/Order.java", ENTITY)


# ---------------------------------------------------------------------------- queries


def test_definitions_one_per_field_declarator_and_at_any_depth() -> None:
    result = _parse("entity/Order.java", ENTITY, [QueryKind.DEFS])
    assert _matches(result, QueryKind.DEFS, "name") == [
        ("definition.class", "Order"),
        ("definition.field", "id"),
        ("definition.field", "customerId"),
        ("definition.field", "status"),
        ("definition.field", "lineCount"),
        ("definition.field", "itemCount"),
        ("definition.constructor", "Order"),
        ("definition.constructor", "Order"),
        ("definition.method", "isOpen"),
        ("definition.method", "compareTo"),
        ("definition.enum", "Status"),
        ("definition.field", "NEW"),
        ("definition.field", "PAID"),
        ("definition.field", "CLOSED"),
        ("definition.field", "code"),
        ("definition.constructor", "Status"),
        ("definition.class", "Line"),
        ("definition.field", "sku"),
        ("definition.class", "Note"),
        ("definition.field", "text"),
    ]
    dto = _parse("dto/OrderDto.java", DTO, [QueryKind.DEFS])
    assert _matches(dto, QueryKind.DEFS, "name") == [
        ("definition.record", "OrderDto"),
        ("definition.field", "customerId"),
        ("definition.field", "total"),
        ("definition.constructor", "OrderDto"),
        ("definition.method", "from"),
    ]
    audited = _parse("audit/Audited.java", AUDITED, [QueryKind.DEFS])
    assert _matches(audited, QueryKind.DEFS, "name") == [
        ("definition.annotation", "Audited"),
        ("definition.method", "value"),
        ("definition.field", "LEVEL"),
    ]


def test_imports_static_imports_on_demand_and_the_package() -> None:
    result = _parse("controller/OrderController.java", CONTROLLER, [QueryKind.IMPORTS])
    assert _matches(result, QueryKind.IMPORTS, "source", "name") == [
        ("import.package", "", "com.acme.shop.controller"),
        ("import", "com.acme.shop.dto", "OrderDto"),
        ("import", "com.acme.shop.service", "OrderService"),
        ("import", "java.util", "List"),
        ("import.wildcard", "org.springframework.web.bind.annotation", "*"),
        ("import.static", "org.springframework.http.HttpStatus", "CREATED"),
    ]
    more = parse_file(
        "A.java",
        "package app;\nimport static org.junit.Assert.*;\nimport app.Outer.Inner;\n",
        kinds=[QueryKind.IMPORTS],
    )
    assert _matches(more, QueryKind.IMPORTS, "source", "name") == [
        ("import.package", "", "app"),
        ("import.static.wildcard", "org.junit.Assert", "*"),
        ("import", "app.Outer", "Inner"),
    ]


def test_calls_with_receivers_creations_constructor_calls_and_method_references() -> None:
    service = _parse("service/OrderService.java", SERVICE, [QueryKind.CALLS])
    assert _matches(service, QueryKind.CALLS, "receiver", "name", "arguments") == [
        (
            "call.method",
            "orderRepository.findByCustomerId(customerId).stream().map(OrderDto::from)",
            "toList",
            "()",
        ),
        (
            "call.method",
            "orderRepository.findByCustomerId(customerId).stream()",
            "map",
            "(OrderDto::from)",
        ),
        ("call.method", "orderRepository.findByCustomerId(customerId)", "stream", "()"),
        ("call.method", "orderRepository", "findByCustomerId", "(customerId)"),
        ("call.reference", "OrderDto", "from", ""),
        ("call", "", "place", "(body, null)"),
        ("call.new", "", "Order", "(body.customerId(), Order.Status.NEW)"),
        ("call.method", "body", "customerId", "()"),
        ("call.method", "paymentClient", "authorize", "(order.getId(), body.total(), coupon)"),
        ("call.method", "order", "getId", "()"),
        ("call.method", "body", "total", "()"),
        ("call.method", "OrderDto", "from", "(orderRepository.save(order))"),
        ("call.method", "orderRepository", "save", "(order)"),
    ]
    entity = _parse("entity/Order.java", ENTITY, [QueryKind.CALLS])
    constructors = [
        m
        for m in _matches(entity, QueryKind.CALLS, "name", "arguments")
        if m[0] == "call.constructor"
    ]
    assert constructors == [
        ("call.constructor", "this", "(customerId, status, 0)"),
        ("call.constructor", "super", "()"),
    ]
    generic = parse_file(
        "A.java",
        "class A { Object f() { list.forEach(Item::new); return new java.util.ArrayList<>(); } }",
        kinds=[QueryKind.CALLS],
    )
    assert _matches(generic, QueryKind.CALLS, "receiver", "name") == [
        ("call.method", "list", "forEach"),
        ("call.reference.new", "", "Item"),
        ("call.new", "", "java.util.ArrayList"),
    ]


def test_annotations_and_what_they_annotate() -> None:
    result = _parse("controller/OrderController.java", CONTROLLER, [QueryKind.DECORATORS])
    found: list[tuple[str, str, str, str]] = []
    for match in result.captures[QueryKind.DECORATORS]:
        target = decorated(match.node)
        assert target is not None
        if target.type == "field_declaration":
            target = target.child_by_field_name("declarator")
        assert target is not None
        name = target.child_by_field_name("name")
        arguments = match.role("arguments")
        found.append(
            (
                result.text(match.roles["callee"][0]),
                result.text(arguments) if arguments is not None else "",
                target.type,
                result.text(name) if name is not None else "",
            )
        )
    assert found == [
        ("RestController", "", "class_declaration", "OrderController"),
        ("RequestMapping", '("/api/orders")', "class_declaration", "OrderController"),
        ("RequiredArgsConstructor", "", "class_declaration", "OrderController"),
        ("GetMapping", "", "method_declaration", "getOrders"),
        ("RequestParam", '("customer")', "formal_parameter", "customerId"),
        ("GetMapping", '("/{id}")', "method_declaration", "getOrder"),
        ("PathVariable", "", "formal_parameter", "id"),
        ("PostMapping", "", "method_declaration", "createOrder"),
        ("ResponseStatus", "(CREATED)", "method_declaration", "createOrder"),
        ("Valid", "", "formal_parameter", "body"),
        ("RequestBody", "", "formal_parameter", "body"),
    ]
    entity = _parse("entity/Order.java", ENTITY, [QueryKind.DECORATORS])
    callees = [m[1] for m in _matches(entity, QueryKind.DECORATORS, "callee")]
    assert callees[:6] == ["Entity", "Table", "Getter", "NoArgsConstructor", "Id", "GeneratedValue"]
    assert "Deprecated" in callees  # on an enum constant
    scoped = parse_file("A.java", "@org.acme.Audited class A {}", kinds=[QueryKind.DECORATORS])
    assert _matches(scoped, QueryKind.DECORATORS, "callee") == [("decorator", "org.acme.Audited")]


def test_strings_and_text_blocks() -> None:
    result = _parse("repository/OrderRepository.java", REPOSITORY, [QueryKind.STRINGS])
    assert _matches(result, QueryKind.STRINGS) == [("string",), ("string",)]
    texts = [result.text(m.node) for m in result.captures[QueryKind.STRINGS]]
    assert texts == ['"select o from Order o where o.status = :status"', '"status"']
    block = parse_file(
        "A.java", 'class A { String q = """\n    select 1\n    """; }', kinds=[QueryKind.STRINGS]
    )
    assert [m.kind for m in block.captures[QueryKind.STRINGS]] == ["string.text_block"]


def test_references_tell_fields_methods_and_types_apart() -> None:
    result = parse_file(
        "A.java", "class A { X f() { return repo.find(this.id); } }", kinds=[QueryKind.REFS]
    )
    assert [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.REFS]] == [
        ("reference.identifier", "A"),
        ("reference.type", "X"),
        ("reference.identifier", "f"),
        ("reference.identifier", "repo"),
        ("reference.method", "find"),
        ("reference.field", "id"),
    ]


# ---------------------------------------------------------------------------- symbols


def test_keys_kinds_subkinds_and_qualified_names(entity: dict[str, Symbol]) -> None:
    found = {key: (s.kind, s.attrs.get("subkind"), s.qualified_name) for key, s in entity.items()}
    assert found == {
        "": (SymbolKind.MODULE, None, "src.main.java.com.acme.shop.entity.Order"),
        "Order#": (SymbolKind.CLASS, None, "Order"),
        "Order#id.": (SymbolKind.VARIABLE, "field", "Order.id"),
        "Order#customerId.": (SymbolKind.VARIABLE, "field", "Order.customerId"),
        "Order#status.": (SymbolKind.VARIABLE, "field", "Order.status"),
        "Order#lineCount.": (SymbolKind.VARIABLE, "field", "Order.lineCount"),
        "Order#itemCount.": (SymbolKind.VARIABLE, "field", "Order.itemCount"),
        "Order#Order().": (SymbolKind.METHOD, "constructor", "Order.Order"),
        "Order#Order().~1": (SymbolKind.METHOD, "constructor", "Order.Order"),
        "Order#isOpen().": (SymbolKind.METHOD, None, "Order.isOpen"),
        "Order#compareTo().": (SymbolKind.METHOD, None, "Order.compareTo"),
        "Order#Status#": (SymbolKind.CLASS, "enum", "Order.Status"),
        "Order#Status#NEW.": (SymbolKind.VARIABLE, "field", "Order.Status.NEW"),
        "Order#Status#PAID.": (SymbolKind.VARIABLE, "field", "Order.Status.PAID"),
        "Order#Status#CLOSED.": (SymbolKind.VARIABLE, "field", "Order.Status.CLOSED"),
        "Order#Status#code.": (SymbolKind.VARIABLE, "field", "Order.Status.code"),
        "Order#Status#Status().": (SymbolKind.METHOD, "constructor", "Order.Status.Status"),
        "Order#Line#": (SymbolKind.CLASS, None, "Order.Line"),
        "Order#Line#sku.": (SymbolKind.VARIABLE, "field", "Order.Line.sku"),
        "Order#Line#Note#": (SymbolKind.CLASS, None, "Order.Line.Note"),
        "Order#Line#Note#text.": (SymbolKind.VARIABLE, "field", "Order.Line.Note.text"),
    }
    prefix = f"java {ROOT}/entity/Order.java/"
    assert entity["Order#Line#Note#"].container_key == f"{prefix}Order#Line#"
    assert entity["Order#"].container_key == prefix
    assert entity[""].name == "Order"


def test_the_controllers_methods_are_keyed_under_their_class(
    controller: dict[str, Symbol],
) -> None:
    assert list(controller) == [
        "",
        "OrderController#",
        "OrderController#orderService.",
        "OrderController#getOrders().",
        "OrderController#getOrder().",
        "OrderController#createOrder().",
    ]
    key = controller["OrderController#getOrders()."].symbol_key
    assert key == f"java {ROOT}/controller/OrderController.java/OrderController#getOrders()."


def test_spans_include_annotations_not_the_javadoc(controller: dict[str, Symbol]) -> None:
    spans = {key: (s.start_line, s.end_line) for key, s in controller.items()}
    assert spans == {
        "": (1, 40),
        "OrderController#": (14, 40),
        "OrderController#orderService.": (19, 19),
        "OrderController#getOrders().": (22, 25),
        "OrderController#getOrder().": (27, 30),
        "OrderController#createOrder().": (34, 39),
    }
    field = controller["OrderController#orderService."]
    assert (field.start_col, field.end_col) == (4, 44)


def test_a_field_of_several_declarators_spans_its_declarator(entity: dict[str, Symbol]) -> None:
    line, item = entity["Order#lineCount."], entity["Order#itemCount."]
    assert (line.start_line, line.start_col, line.end_col) == (24, 24, 33)
    assert (item.start_line, item.start_col, item.end_col) == (24, 35, 44)
    # Both carry their declaration's annotations, Javadoc and modifiers.
    assert line.attrs["decorators"] == item.attrs["decorators"] == ["@Column"]
    assert line.docstring == item.docstring == "Counted when the order is placed."
    assert (line.signature, item.signature) == ("private int lineCount", "private int itemCount")


def test_signatures_stop_at_the_body_without_annotations(
    controller: dict[str, Symbol], entity: dict[str, Symbol]
) -> None:
    signatures = {key: s.signature for key, s in controller.items()}
    assert signatures == {
        "": None,
        "OrderController#": "public class OrderController",
        "OrderController#orderService.": "private final OrderService orderService",
        "OrderController#getOrders().": (
            'public List<OrderDto> getOrders(@RequestParam("customer") Long customerId)'
        ),
        "OrderController#getOrder().": (
            "public ResponseEntity<OrderDto> getOrder(@PathVariable Long id)"
        ),
        "OrderController#createOrder().": (
            "public OrderDto createOrder(@Valid @RequestBody OrderDto body)"
        ),
    }
    assert entity["Order#"].signature == (
        "public class Order extends BaseEntity implements Serializable, Comparable<Order>"
    )
    assert entity["Order#Order().~1"].signature == (
        "public Order(Long customerId, Status status, int lineCount)"
    )
    assert entity["Order#id."].signature == "private Long id"
    assert entity["Order#Status#NEW."].signature == 'NEW("n")'
    assert entity["Order#Status#Status()."].signature == "Status(String code)"


def test_javadoc_and_line_comments_are_docstrings_a_plain_block_comment_isnt(
    controller: dict[str, Symbol], entity: dict[str, Symbol]
) -> None:
    assert controller["OrderController#"].docstring == (
        "Orders, over HTTP.\n\n<p>Thin: the service does the work."
    )
    assert controller["OrderController#getOrders()."].docstring == "Every order of a customer."
    assert controller["OrderController#createOrder()."].docstring == (
        "Places an order.\nAnswers 201."
    )
    assert controller["OrderController#getOrder()."].docstring is None
    assert entity["Order#"].docstring is None  # the licence above it
    assert entity["Order#Status#"].docstring == "Where an order is."


def test_annotations_are_decorators(
    controller: dict[str, Symbol], entity: dict[str, Symbol]
) -> None:
    assert controller["OrderController#"].attrs["decorators"] == [
        "@RestController",
        '@RequestMapping("/api/orders")',
        "@RequiredArgsConstructor",
    ]
    assert controller["OrderController#createOrder()."].attrs["decorators"] == [
        "@PostMapping",
        "@ResponseStatus(CREATED)",
    ]
    assert "decorators" not in controller["OrderController#orderService."].attrs
    assert entity["Order#"].attrs["decorators"] == [
        "@Entity",
        '@Table(name = "orders")',
        "@Getter",
        "@NoArgsConstructor",
    ]
    assert entity["Order#id."].attrs["decorators"] == [
        "@Id",
        "@GeneratedValue(strategy = GenerationType.IDENTITY)",
    ]
    assert entity["Order#customerId."].attrs["decorators"] == [
        '@Column(name = "customer_id", nullable = false)'
    ]
    assert entity["Order#Status#CLOSED."].attrs["decorators"] == ["@Deprecated"]
    assert entity["Order#compareTo()."].attrs["decorators"] == ["@Override"]


def test_bases_keep_their_type_arguments(entity: dict[str, Symbol]) -> None:
    assert entity["Order#"].attrs["bases"] == ["BaseEntity", "Serializable", "Comparable<Order>"]
    repository = _symbols("repository/OrderRepository.java", REPOSITORY)
    assert repository["OrderRepository#"].attrs == {
        "subkind": "interface",
        "bases": ["JpaRepository<Order, Long>", "OrderSearch"],
    }
    assert _symbols("dto/OrderDto.java", DTO)["OrderDto#"].attrs["bases"] == ["Serializable"]


def test_interfaces_with_abstract_and_default_methods() -> None:
    repository = _symbols("repository/OrderRepository.java", REPOSITORY)
    methods = {
        key: (s.kind, s.signature) for key, s in repository.items() if s.kind is SymbolKind.METHOD
    }
    assert methods == {
        "OrderRepository#findByCustomerId().": (
            SymbolKind.METHOD,
            "List<Order> findByCustomerId(Long customerId)",
        ),
        "OrderRepository#findByStatus().": (
            SymbolKind.METHOD,
            'List<Order> findByStatus(@Param("status") Order.Status status)',
        ),
        "OrderRepository#isOpen().": (SymbolKind.METHOD, "default boolean isOpen(Long id)"),
    }
    query = repository["OrderRepository#findByStatus()."]
    assert query.attrs["decorators"] == ['@Query("select o from Order o where o.status = :status")']
    assert (query.start_line, query.end_line) == (10, 11)


def test_records_their_components_and_compact_constructor() -> None:
    dto = _symbols("dto/OrderDto.java", DTO)
    found = {key: (s.kind, s.attrs.get("subkind"), s.signature) for key, s in dto.items()}
    assert found == {
        "": (SymbolKind.MODULE, None, None),
        "OrderDto#": (
            SymbolKind.CLASS,
            "record",
            "public record OrderDto(@NotNull Long customerId, BigDecimal total)"
            " implements Serializable",
        ),
        "OrderDto#customerId.": (SymbolKind.VARIABLE, "field", "Long customerId"),
        "OrderDto#total.": (SymbolKind.VARIABLE, "field", "BigDecimal total"),
        "OrderDto#OrderDto().": (SymbolKind.METHOD, "constructor", "public OrderDto"),
        "OrderDto#from().": (SymbolKind.METHOD, None, "public static OrderDto from(Order order)"),
    }
    assert dto["OrderDto#customerId."].attrs["decorators"] == ["@NotNull"]


def test_annotation_types() -> None:
    audited = _symbols("audit/Audited.java", AUDITED)
    assert audited["Audited#"].attrs == {
        "subkind": "annotation",
        "decorators": [
            "@Retention(RetentionPolicy.RUNTIME)",
            "@Target({ElementType.METHOD, ElementType.TYPE})",
        ],
    }
    assert audited["Audited#"].signature == "public @interface Audited"
    assert audited["Audited#value()."].signature == 'String value() default ""'
    assert audited["Audited#LEVEL."].signature == "int LEVEL = 1"


def test_overloads_are_numbered_in_source_order_and_one_added_after_moves_none() -> None:
    service = _symbols("service/OrderService.java", SERVICE)
    assert service["OrderService#place()."].signature == "public OrderDto place(OrderDto body)"
    assert service["OrderService#place().~1"].signature == (
        "public OrderDto place(OrderDto body, String coupon)"
    )
    added = SERVICE.replace(
        "\n}\n",
        "\n    public OrderDto place(Long customerId) {\n        return null;\n    }\n}\n",
    )
    after = _symbols("service/OrderService.java", added)
    assert set(after) == set(service) | {"OrderService#place().~2"}
    for key, symbol in service.items():
        if key not in ("", "OrderService#"):  # the file and the class hold the new one
            assert after[key].content_hash == symbol.content_hash, key


def test_keys_survive_moved_lines() -> None:
    before = _symbols("dto/OrderDto.java", DTO)
    after = _symbols(
        "dto/OrderDto.java",
        DTO.replace(
            "package com.acme.shop.dto;\n",
            "package com.acme.shop.dto;\n\nimport java.util.Objects;\n\n",
        ),
    )
    assert set(before) == set(after)
    assert before["OrderDto#from()."].content_hash == after["OrderDto#from()."].content_hash
    assert before["OrderDto#from()."].start_line + 3 == after["OrderDto#from()."].start_line


# ---------------------------------------------------------------------------- end to end


def test_a_java_file_is_parsed_keyed_and_chunked_by_definition() -> None:
    path = f"{ROOT}/service/OrderService.java"
    steps = "".join(
        f'        audit.record("step-{i}", body.customerId(), body.total(), {i});\n'
        for i in range(12)
    )
    source = SERVICE.replace(
        "        return place(body, null);\n", steps + "        return place(body, null);\n"
    )
    source = source.replace("        Order order = new", steps + "        Order order = new")
    result = parse_file(path, source.encode())  # the language comes from the path
    assert (result.status, result.language) == (ParseStatus.PARSED, "java")
    assert all(
        result.captures[kind]
        for kind in (QueryKind.DEFS, QueryKind.IMPORTS, QueryKind.CALLS, QueryKind.DECORATORS)
    )
    symbols = extract_symbols(result, "java")
    assert symbols[0].symbol_key == f"java {path}/"
    chunks = chunk_file(path, "java", source)
    keyed = [(c.symbol_key or "").removeprefix(f"java {path}/") for c in chunks]
    # The imports, the class's header (its fields), then its methods, each a chunk.
    assert keyed == [
        "",
        "OrderService#",
        "OrderService#findByCustomer().",
        "OrderService#place().",
        "OrderService#place().~1",
    ]
    assert [c.kind.value for c in chunks] == ["code", "class", "method", "method", "method"]
    assert "lang: java" in chunks[1].header
    assert "private final OrderRepository orderRepository;" in chunks[1].content
    assert chunks[3].content.lstrip().startswith("@Transactional")


# ---------------------------------------------------------------------------- lanes


@pytest.mark.parametrize(
    ("package", "code", "expected"),
    [
        ("controller/OrderController.java", "@RestController\nclass OrderController {}", "api"),
        ("controller/ApiErrors.java", "@RestControllerAdvice\nclass ApiErrors {}", "api"),
        ("web/LoggingFilter.java", "class LoggingFilter extends OncePerRequestFilter {}", "api"),
        ("web/rest/OrderResource.java", "class OrderResource {}", "api"),
        ("service/OrderService.java", "@Service\nclass OrderService {}", "svc"),
        ("services/impl/OrderServiceImpl.java", "class OrderServiceImpl {}", "svc"),
        ("domain/service/PricingService.java", "class PricingService {}", "svc"),
        ("repository/OrderRepository.java", "interface OrderRepository {}", "db"),
        ("repositories/Search.java", "class Search {}", "db"),
        ("dao/OrderDao.java", "@Repository\nclass OrderDao {}", "db"),
        ("entity/Order.java", "@Entity\nclass Order {}", "db"),
        ("entities/Address.java", "@Embeddable\nclass Address {}", "db"),
        ("model/OrderStatus.java", "enum OrderStatus { NEW }", "db"),
        ("models/Money.java", "class Money {}", "db"),
        ("domain/Customer.java", "class Customer {}", "db"),
        ("OrderServiceApplication.java", "@SpringBootApplication\nclass App {}", "svc"),
    ],
)
def test_spring_packages_fall_in_their_lanes(package: str, code: str, expected: str) -> None:
    path = f"{ROOT}/{package}"
    role = assign_role(path, "java", code, ui_package=False)
    assert lane("class", path, role.value) == expected
