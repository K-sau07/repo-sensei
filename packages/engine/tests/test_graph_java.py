"""Java calls, imports, bases and Spring's dependency injection across files (3.18): a layered
Spring Boot app of our own (controller, service interface and implementation, repository,
entity, a static util, records), every edge checked with its provenance and confidence; the
facts the parse pool keeps; the attrs 3.19 and 3.20 read; and the graph stage running Java end
to end, through to a trace that reaches the implementation."""

from __future__ import annotations

import pickle
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from sensei_core.models import EdgeType, Provenance
from sensei_engine.graph import java
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.java.facts import JavaFacts
from sensei_engine.graph.project import Definition, Project
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node, lane
from sensei_engine.graph.stage import (
    RESOLVERS,
    GraphRequest,
    extract_request,
    resolve_graph,
    resolver_for,
)
from sensei_engine.graph.trace import trace_feature
from sensei_engine.graph.writer import CODE, GraphEdge, GraphNode
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols

ROOT = "src/main/java/com/example/store"
JPA = "org.springframework.data.jpa.repository.JpaRepository"
LONG = "external_symbol java:java.lang.Long"

FILES = {
    "domain/Cart.java": """\
package com.example.store.domain;

import jakarta.persistence.*;
import java.util.ArrayList;
import java.util.List;
import lombok.Getter;

@Entity
@Table(name = "carts")
@Getter
public class Cart {
    @Id
    private Long id;

    @ManyToOne
    private Customer owner;

    @OneToMany(mappedBy = "cart")
    private List<Line> lines = new ArrayList<>();

    public Customer getOwner() {
        return owner;
    }

    public List<Line> getLines() {
        return lines;
    }

    public void add(Line line) {
        lines.add(line);
    }

    public void touch() {
    }

    public static class Line {
        private final String sku;
        private final int quantity;

        public Line(String sku, int quantity) {
            this.sku = sku;
            this.quantity = quantity;
        }
    }
}
""",
    "domain/Customer.java": """\
package com.example.store.domain;

public class Customer {
    private String name;

    public String getName() {
        return name;
    }

    public void touch() {
    }
}
""",
    "dto/AddItem.java": """\
package com.example.store.dto;

public record AddItem(Long cartId, String sku, int quantity) {
}
""",
    "dto/CartView.java": """\
package com.example.store.dto;

import com.example.store.domain.Cart;

public record CartView(Long id, int lines) {
    public static CartView of(Cart cart) {
        return new CartView(cart.getId(), cart.getLines().size());
    }
}
""",
    "repository/CartRepository.java": """\
package com.example.store.repository;

import com.example.store.domain.Cart;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface CartRepository extends JpaRepository<Cart, Long> {
    List<Cart> findByOwnerName(String name);
}
""",
    "service/AuditLog.java": """\
package com.example.store.service;

import org.springframework.stereotype.Component;

@Component
public class AuditLog {
    public void record(String action) {
    }

    public void record(String action, int count) {
    }

    public void record(String action, String detail) {
    }
}
""",
    "service/BaseService.java": """\
package com.example.store.service;

import com.example.store.domain.Cart;

public abstract class BaseService {
    protected void check(Cart cart) {
    }
}
""",
    "service/CartService.java": """\
package com.example.store.service;

import com.example.store.domain.Cart;

public interface CartService {
    Cart load(Long id);

    Cart addItem(Long cartId, String sku, int quantity);

    Cart addItem(Long cartId, String sku);
}
""",
    "service/CartServiceImpl.java": """\
package com.example.store.service;

import com.example.store.domain.Cart;
import com.example.store.repository.CartRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

@Service
@RequiredArgsConstructor
public class CartServiceImpl extends BaseService implements CartService {
    private final CartRepository carts;
    private final AuditLog audit;

    @Override
    public Cart load(Long id) {
        return carts.findById(id).orElseThrow();
    }

    @Override
    public Cart addItem(Long cartId, String sku, int quantity) {
        Cart cart = load(cartId);
        cart.add(new Cart.Line(sku, quantity));
        audit.record("add");
        audit.record("add", quantity);
        check(cart);
        return carts.save(cart);
    }

    @Override
    public Cart addItem(Long cartId, String sku) {
        return addItem(cartId, sku, 1);
    }

    @Override
    protected void check(Cart cart) {
        super.check(cart);
        carts.findByOwnerName(cart.getOwner().getName());
    }
}
""",
    "service/PricingService.java": """\
package com.example.store.service;

import com.example.store.domain.Cart;
import java.util.List;
import org.springframework.stereotype.Service;

import static com.example.store.util.Money.*;

@Service
public class PricingService {
    public double total(Cart cart) {
        return round(this.discount(cart));
    }

    public List<Double> totals(List<Cart> carts) {
        carts.forEach(c -> c.touch());
        return carts.stream().map(this::total).toList();
    }

    private double discount(Cart cart) {
        return cart.getLines().size() * 0.9;
    }
}
""",
    "util/Money.java": """\
package com.example.store.util;

public final class Money {
    private Money() {
    }

    public static long round(double amount) {
        return Math.round(amount);
    }

    public static String format(long cents) {
        return String.valueOf(cents);
    }
}
""",
    "web/CartController.java": """\
package com.example.store.web;

import com.example.store.dto.*;
import com.example.store.service.CartService;
import com.example.store.service.PricingService;
import com.example.store.util.Money;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.*;

import static com.example.store.util.Money.round;

@RestController
@RequestMapping("/api/carts")
public class CartController {
    private final CartService cartService;

    @Autowired
    private PricingService pricing;

    public CartController(CartService cartService) {
        this.cartService = cartService;
    }

    @GetMapping("/{id}")
    public CartView get(@PathVariable Long id) {
        return CartView.of(cartService.load(id));
    }

    @PostMapping
    public CartView add(@RequestBody AddItem body) {
        var cart = cartService.addItem(body.cartId(), body.sku(), body.quantity());
        long total = round(pricing.total(cart));
        Money.format(total);
        cartService.load(body.cartId()).getOwner().getName();
        unknownThing.frobnicate();
        return CartView.of(cart);
    }
}
""",
}


def _key(rest: str) -> str:
    return f"java {ROOT}/{rest}"


def _short(key: str) -> str:
    return key.removeprefix(f"java {ROOT}/").replace("external_symbol java:", "java:")


def _facts(files: Mapping[str, str], root: str = ROOT) -> dict[str, JavaFacts]:
    found: dict[str, JavaFacts] = {}
    for name, content in files.items():
        parsed = parse_file(f"{root}/{name}", content.encode())
        facts = java.extract(parsed)
        assert facts is not None, name
        found[f"{root}/{name}"] = facts
    return found


def _resolve(
    files: Mapping[str, str], root: str = ROOT
) -> tuple[list[GraphNode], list[GraphEdge], java.JavaStats]:
    facts = _facts(files, root)
    nodes, edges, stats = java.resolve(Project(frozenset(facts)), facts)
    CODE.check(nodes, edges)  # the graph stage's layer
    externals = {e.dst for e in edges if not e.dst.startswith("java ")}
    assert externals <= {n.key for n in nodes}  # every external has its node
    assert all(e.owner == e.evidence_path for e in edges)
    return nodes, edges, stats


def _found(edges: list[GraphEdge], name: str) -> set[tuple[Any, ...]]:
    """A file's edges but its imports: (line, type, source, target, provenance, confidence)."""
    return {
        (
            e.evidence_line,
            e.type.value,
            _short(e.src).split("/")[-1],
            _short(e.dst),
            e.provenance.value,
            round(e.confidence if e.confidence is not None else confidence_of(e.provenance), 3),
        )
        for e in edges
        if e.evidence_path == f"{ROOT}/{name}" and e.type is not EdgeType.IMPORTS
    }


@pytest.fixture(scope="module")
def app() -> tuple[list[GraphNode], list[GraphEdge], java.JavaStats]:
    return _resolve(FILES)


IR, RH = "import_resolved", "receiver_heuristic"
CART, CUSTOMER = "domain/Cart.java/Cart#", "domain/Customer.java/Customer#"
SERVICE, IMPL = (
    "service/CartService.java/CartService#",
    "service/CartServiceImpl.java/CartServiceImpl#",
)
REPOSITORY = "repository/CartRepository.java/CartRepository#"
PRICING, AUDIT = "service/PricingService.java/PricingService#", "service/AuditLog.java/AuditLog#"
VIEW, ADD = "dto/CartView.java/CartView#", "dto/AddItem.java/AddItem#"
MONEY, BASE = "util/Money.java/Money#", "service/BaseService.java/BaseService#"


def test_the_controller_calls_the_service_interface_through_its_injected_fields(app: Any) -> None:
    _, edges, _ = app
    assert _found(edges, "web/CartController.java") == {
        # field injection (@Autowired) and constructor injection, class to the injected type
        (17, "injects", "CartController#", PRICING, IR, 0.9),
        (20, "injects", "CartController#", SERVICE, IR, 0.9),
        # the interface's one implementation is the bean Spring gives
        (20, "injects", "CartController#", IMPL, RH, 0.7),
        # a static call through a wildcard import, a typed field: the interface's method
        (26, "calls", "CartController#get().", f"{VIEW}of().", IR, 0.9),
        (26, "calls", "CartController#get().", f"{SERVICE}load().", IR, 0.9),
        # three arguments: the three-parameter overload; a record's components are its fields
        (31, "calls", "CartController#add().", f"{SERVICE}addItem().", IR, 0.9),
        (31, "references", "CartController#add().", f"{ADD}cartId.", IR, 0.9),
        (31, "references", "CartController#add().", f"{ADD}sku.", IR, 0.9),
        (31, "references", "CartController#add().", f"{ADD}quantity.", IR, 0.9),
        # round: a static import; pricing: the @Autowired field
        (32, "calls", "CartController#add().", f"{MONEY}round().", IR, 0.9),
        (32, "calls", "CartController#add().", f"{PRICING}total().", IR, 0.9),
        (33, "calls", "CartController#add().", f"{MONEY}format().", IR, 0.9),
        # a chain: getOwner on what load returns (one level), then getName by its name only
        (34, "calls", "CartController#add().", f"{SERVICE}load().", IR, 0.9),
        (34, "references", "CartController#add().", f"{ADD}cartId.", IR, 0.9),
        (34, "calls", "CartController#add().", f"{CART}getOwner().", RH, 0.7),
        (34, "calls", "CartController#add().", f"{CUSTOMER}getName().", "name_unique", 0.4),
        # unknownThing.frobnicate(): nothing binds it, no method has the name: no edge
        (36, "calls", "CartController#add().", f"{VIEW}of().", IR, 0.9),
    }  # fmt: skip


def test_the_implementation_reaches_its_repository_and_the_library_methods_it_inherits(
    app: Any,
) -> None:
    _, edges, _ = app
    receiver = {"receiver": _key(REPOSITORY)}
    found = _found(edges, "service/CartServiceImpl.java")
    assert found == {
        (8, "inherits", "CartServiceImpl#", BASE, IR, 0.9),
        (8, "implements", "CartServiceImpl#", SERVICE, IR, 0.9),
        # @RequiredArgsConstructor on a @Service: its final fields are injected
        (11, "injects", "CartServiceImpl#", REPOSITORY, IR, 0.9),
        (12, "injects", "CartServiceImpl#", AUDIT, IR, 0.9),
        # findById: JpaRepository's (orElseThrow on what a library returns: not followed)
        (16, "calls", "CartServiceImpl#load().", f"java:{JPA}.findById", IR, 0.9),
        (21, "calls", "CartServiceImpl#addItem().", f"{IMPL}load().", IR, 0.9),  # implicit this
        # a nested class's constructor, then a method of the local's declared type
        (22, "calls", "CartServiceImpl#addItem().", f"{CART}Line#Line().", IR, 0.9),
        (22, "calls", "CartServiceImpl#addItem().", f"{CART}add().", IR, 0.9),
        # overloads: by count (one argument), then by type (int, not String)
        (23, "calls", "CartServiceImpl#addItem().", f"{AUDIT}record().", IR, 0.9),
        (24, "calls", "CartServiceImpl#addItem().", f"{AUDIT}record().~1", IR, 0.9),
        (25, "calls", "CartServiceImpl#addItem().", f"{IMPL}check().", IR, 0.9),  # the override
        (26, "calls", "CartServiceImpl#addItem().", f"java:{JPA}.save", IR, 0.9),
        (31, "calls", "CartServiceImpl#addItem().~1", f"{IMPL}addItem().", IR, 0.9),
        (36, "calls", "CartServiceImpl#check().", f"{BASE}check().", IR, 0.9),  # super
        (37, "calls", "CartServiceImpl#check().", f"{REPOSITORY}findByOwnerName().", IR, 0.9),
        (37, "calls", "CartServiceImpl#check().", f"{CART}getOwner().", IR, 0.9),
        (37, "calls", "CartServiceImpl#check().", f"{CUSTOMER}getName().", RH, 0.7),
    }  # fmt: skip
    inherited = [e for e in edges if e.dst.startswith(f"external_symbol java:{JPA}.")]
    assert {e.dst.rsplit(".", 1)[-1] for e in inherited} == {"findById", "save"}
    assert all({k: e.attrs[k] for k in receiver} == receiver for e in inherited)


def test_static_imports_this_references_and_unknown_receivers(app: Any) -> None:
    _, edges, _ = app
    assert _found(edges, "service/PricingService.java") == {
        # this.discount; round through import static ...Money.*
        (12, "calls", "PricingService#total().", f"{PRICING}discount().", IR, 0.9),
        (12, "calls", "PricingService#total().", f"{MONEY}round().", IR, 0.9),
        # a JDK type's method: its external; c, a lambda's parameter: touch by name, two
        # classes have it (name_ambiguous, 0.2 / 2 each)
        (16, "calls", "PricingService#totals().", "java:java.util.List.forEach", IR, 0.9),
        (16, "calls", "PricingService#totals().", f"{CART}touch().", "name_ambiguous", 0.1),
        (16, "calls", "PricingService#totals().", f"{CUSTOMER}touch().", "name_ambiguous", 0.1),
        # a method reference is a call of the method; map() on what stream() returns: not followed
        (17, "calls", "PricingService#totals().", "java:java.util.List.stream", IR, 0.9),
        (17, "calls", "PricingService#totals().", f"{PRICING}total().", IR, 0.9),
        # cart.getLines() is declared to return a List: size() is List's, one level on
        (21, "calls", "PricingService#discount().", f"{CART}getLines().", IR, 0.9),
        (21, "calls", "PricingService#discount().", "java:java.util.List.size", RH, 0.7),
    }  # fmt: skip
    reference = next(e for e in edges if e.evidence_line == 17 and e.dst.endswith("total()."))
    assert reference.attrs["reference"] is True
    assert _found(edges, "dto/CartView.java") == {
        # a record with no constructor of its own: the class; Lombok's @Getter getId: the field
        (7, "calls", "CartView#of().", VIEW, IR, 0.9),
        (7, "references", "CartView#of().", f"{CART}id.", IR, 0.9),
        (7, "calls", "CartView#of().", f"{CART}getLines().", IR, 0.9),
        (7, "calls", "CartView#of().", "java:java.util.List.size", RH, 0.7),
    }  # fmt: skip


def test_imports_resolve_by_package_with_their_externals(app: Any) -> None:
    nodes, edges, stats = app
    imports = {
        (e.evidence_line, _short(e.dst), bool(e.attrs.get("static")))
        for e in edges
        if e.type is EdgeType.IMPORTS and e.evidence_path == f"{ROOT}/web/CartController.java"
    }
    assert imports == {
        # import com.example.store.dto.*: the package's types the file uses
        (3, ADD, False),
        (3, VIEW, False),
        (4, SERVICE, False),
        (5, PRICING, False),
        (6, MONEY, False),
        (7, "java:org.springframework.beans.factory.annotation.Autowired", False),
        (8, "package java:org.springframework.web.bind.annotation", False),  # a library's *
        (10, f"{MONEY}round().", True),  # import static ...Money.round
    }
    controller = f"{ROOT}/web/CartController.java"
    controller_imports = [
        e for e in edges if e.type is EdgeType.IMPORTS and e.evidence_path == controller
    ]
    assert {e.src for e in controller_imports} == {_key("web/CartController.java/")}
    assert (stats.internal, stats.resolved, stats.unresolved) == (13, 13, [])
    assert stats.external == 13
    by_key = {n.key: n for n in nodes}
    save = by_key[f"external_symbol java:{JPA}.save"]
    assert (save.name, save.qualified_name, save.lang) == ("save", f"{JPA}.save", "java")
    assert save.attrs == {
        "package": "org.springframework.data.jpa.repository",
        "fqn": f"{JPA}.save",
    }
    controller = stats.tables[_key("web/CartController.java/")]
    assert controller["CartService"] == _key(SERVICE)
    assert controller["round"] == _key(f"{MONEY}round().")


def test_calls_stats_count_every_tier(app: Any) -> None:
    _, _, stats = app
    calls = stats.calls
    assert set(calls.by_provenance) == {
        Provenance.IMPORT_RESOLVED, Provenance.RECEIVER_HEURISTIC, Provenance.NAME_UNIQUE,
        Provenance.NAME_AMBIGUOUS,
    }  # fmt: skip
    assert (calls.guessed, calls.unresolved) == (2, 1)  # getName, touch; frobnicate
    assert calls.calls == calls.resolved + calls.external + calls.guessed + calls.unresolved + (
        calls.dead
    )
    assert calls.accessors == 5
    assert (stats.di.beans, stats.di.injections, stats.di.injects) == (4, 4, 4)
    assert stats.di.implementations == 1  # CartService's; CartRepository has no bean
    assert (stats.code.inherits, stats.code.implements, stats.code.external_bases) == (1, 1, 1)


def test_the_attrs_spring_routes_and_jpa_read(app: Any) -> None:
    _, _, stats = app
    attrs = {key: held["java"] for key, held in stats.attrs.items()}
    repository = attrs[_key(REPOSITORY)]
    assert repository["fqn"] == "com.example.store.repository.CartRepository"
    assert repository["bases"] == [
        {
            "written": "JpaRepository<Cart, Long>",
            "relation": "extends",
            "target": f"external_symbol java:{JPA}",
            "fqn": JPA,
            "external": True,
            "arguments": [
                {"written": "Cart", "target": _key(CART), "fqn": "com.example.store.domain.Cart"},
                {"written": "Long", "target": LONG, "fqn": "java.lang.Long"},
            ],
        }
    ]  # fmt: skip
    assert repository["repository"]["entity"]["target"] == _key(CART)
    get = attrs[_key("web/CartController.java/CartController#get().")]
    assert get["annotations"] == [
        {
            "name": "GetMapping",
            "line": 24,
            "fqn": "org.springframework.web.bind.annotation.GetMapping",  # through the *
            "arguments": '("/{id}")',
        }
    ]
    assert get["parameters"] == [
        {
            "name": "id",
            "type": "Long",
            "target": "external_symbol java:java.lang.Long",
            "fqn": "java.lang.Long",
            "annotations": [
                {
                    "name": "PathVariable",
                    "line": 25,
                    "fqn": "org.springframework.web.bind.annotation.PathVariable",
                }
            ],
        }
    ]
    assert get["returns_target"] == _key(VIEW)
    controller = attrs[_key("web/CartController.java/CartController#")]
    assert [a["name"] for a in controller["annotations"]] == ["RestController", "RequestMapping"]
    assert controller["annotations"][1]["arguments"] == '("/api/carts")'
    assert controller["bean"] == "RestController"
    assert controller["injects"] == [
        {"via": "field", "name": "pricing", "type": _key(PRICING)},
        {"via": "constructor", "name": "cartService", "field": "cartService",
         "bean": _key(IMPL), "type": _key(SERVICE)},
    ]  # fmt: skip
    lines = attrs[_key(f"{CART}lines.")]
    assert (lines["type"], lines["target"]) == ("List<Line>", _key(f"{CART}Line#"))
    assert lines["annotations"] == [
        {
            "name": "OneToMany",
            "line": 18,
            "fqn": "jakarta.persistence.OneToMany",
            "arguments": '(mappedBy = "cart")',
        }
    ]
    assert attrs[_key(CART)]["annotations"][1]["arguments"] == '(name = "carts")'


def test_the_parse_pool_keeps_the_files_facts() -> None:
    facts = _facts({"service/CartServiceImpl.java": FILES["service/CartServiceImpl.java"]})
    file = facts[f"{ROOT}/service/CartServiceImpl.java"]
    assert (file.module, file.package) == (
        _key("service/CartServiceImpl.java/"),
        "com.example.store.service",
    )
    assert [(i.kind, i.target) for i in file.imports] == [
        ("single", "com.example.store.domain.Cart"),
        ("single", "com.example.store.repository.CartRepository"),
        ("single", "lombok.RequiredArgsConstructor"),
        ("single", "org.springframework.stereotype.Service"),
    ]
    [impl] = file.types
    assert (impl.kind, impl.extends, impl.implements) == (
        "class",
        ("BaseService",),
        ("CartService",),
    )
    assert [(f.name, f.type, f.final, f.initialized) for f in impl.fields] == [
        ("carts", "CartRepository", True, False),
        ("audit", "AuditLog", True, False),
    ]
    assert [(m.key.rsplit("/", 1)[-1], len(m.parameters)) for m in impl.methods] == [
        ("CartServiceImpl#load().", 1),
        ("CartServiceImpl#addItem().", 3),
        ("CartServiceImpl#addItem().~1", 2),
        ("CartServiceImpl#check().", 1),
    ]
    assert [(v.name, v.type, v.line, v.end_line) for v in file.locals] == [("cart", "Cart", 21, 27)]
    record = next(c for c in file.calls if c.line == 24)
    assert (record.name, record.receiver.parts, record.arguments) == (
        "record", ("audit",), ("=String", "$quantity"),
    )  # fmt: skip
    chained = next(c for c in file.calls if c.name == "orElseThrow")
    assert chained.receiver.kind == "call"
    assert file.calls[chained.receiver.call].name == "findById"
    assert pickle.loads(pickle.dumps(file)) == file  # noqa: S301 - crosses the pool's boundary


# ---------------------------------------------------------------------- smaller cases

SMALL = {
    "a/Shapes.java": """\
package demo.a;

import lombok.Builder;
import lombok.extern.slf4j.Slf4j;

@Slf4j
@Builder
public class Shapes {
    public enum Kind { ROUND, SQUARE }

    private final Kind kind;

    public Shapes(Kind kind) {
        this(kind, 1);
    }

    public Shapes(Kind kind, int sides) {
        this.kind = kind;
    }

    void area(int side) {
    }

    void area(long side) {
    }

    void draw(Object any) {
        var copy = new Shapes(Kind.ROUND);
        copy.area(any.hashCode());
        log.info("drawn");
        Kind.valueOf("ROUND");
        Shapes.builder().kind(kind).build();
        demo.b.Tools.help();
        new Painter().paint();
    }

    class Painter {
        void paint() {
            Shapes.this.area(2);
            area(3L);
        }
    }
}
""",
    "b/Tools.java": """\
package demo.b;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class Tools extends Base {
    public static void help() {
    }

    public String name() {
        return super.toString();
    }

    @Bean
    public Runner runner(Helper helper, java.util.List<Step> steps) {
        return null;
    }
}

abstract class Base {
}

interface Step {
    void go();
}

class Walk implements Step {
    public void go() {
    }
}

class Helper {
}

class Runner {
}
""",
}


def test_smaller_cases() -> None:
    nodes, edges, _ = _resolve(SMALL, root="src")
    found = {
        (e.evidence_line, e.type.value, e.dst.removeprefix("java src/"), e.provenance.value,
         round(e.confidence or 0.9, 3), e.attrs.get("overloads"))
        for e in edges
        if e.type in (EdgeType.CALLS, EdgeType.INJECTS) and e.evidence_path == "src/a/Shapes.java"
    }  # fmt: skip
    shapes = "a/Shapes.java/Shapes#"
    assert found == {
        (14, "calls", f"{shapes}Shapes().~1", IR, 0.9, None),  # this(kind, 1)
        # var copy = new Shapes(...): one argument, the first constructor
        (28, "calls", f"{shapes}Shapes().", IR, 0.9, None),
        # the argument is a call whose type isn't told: both overloads fit, the confidence split
        (29, "calls", f"{shapes}area().", IR, 0.45, 2),
        (29, "calls", f"{shapes}area().~1", IR, 0.45, 2),
        (29, "calls", "external_symbol java:java.lang.Object.hashCode", IR, 0.9, None),
        (30, "calls", "external_symbol java:org.slf4j.Logger.info", IR, 0.9, None),  # @Slf4j
        (31, "calls", "external_symbol java:java.lang.Enum.valueOf", IR, 0.9, None),
        (32, "calls", "a/Shapes.java/Shapes#", IR, 0.9, None),  # Lombok's builder()
        (33, "calls", "b/Tools.java/Tools#help().", IR, 0.9, None),  # fully qualified
        (34, "calls", f"{shapes}Painter#", IR, 0.9, None),  # an inner class, no constructor
        (34, "calls", f"{shapes}Painter#paint().", IR, 0.9, None),
        (39, "calls", f"{shapes}area().", IR, 0.9, None),  # Outer.this; 2 is an int
        (40, "calls", f"{shapes}area().~1", IR, 0.9, None),  # 3L: the long one, the outer's
    }
    tools = {
        (e.evidence_line, e.type.value, _short_src(e.src), e.dst.removeprefix("java src/"))
        for e in edges
        if e.evidence_path == "src/b/Tools.java" and e.type is not EdgeType.IMPORTS
    }
    assert tools == {
        (6, "inherits", "Tools#", "b/Tools.java/Base#"),  # the class's line (its annotations')
        (28, "implements", "Walk#", "b/Tools.java/Step#"),
        # a @Bean method's parameters (a collection: its element type)
        (16, "injects", "Tools#runner().", "b/Tools.java/Helper#"),
        (16, "injects", "Tools#runner().", "b/Tools.java/Step#"),
        # super.toString(): the superclass declares none, nor a library it extends: nothing
    }
    assert {n.key for n in nodes} >= {"external_symbol java:org.slf4j.Logger.info"}


def _short_src(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def test_a_guess_never_takes_a_jdk_name_and_an_override_is_one_candidate() -> None:
    files = {
        "Api.java": "package p;\npublic interface Api {\n    void sync(int n);\n}\n",
        "Impl.java": (
            "package p;\npublic class Impl implements Api {\n    public void sync(int n) {\n"
            "    }\n\n    public void size() {\n    }\n}\n"
        ),
        "Use.java": (
            "package p;\nimport java.util.function.Consumer;\npublic class Use {\n"
            "    void run(java.util.List<Object> xs) {\n"
            "        xs.forEach(x -> { x.sync(1); x.size(); x.sync(); });\n    }\n}\n"
        ),
    }
    _, edges, stats = _resolve(files, root="src")
    calls = {
        (e.dst.removeprefix("java src/"), e.provenance.value, e.confidence)
        for e in edges
        if e.type is EdgeType.CALLS and e.evidence_path == "src/Use.java"
    }
    # sync(1): Api's and Impl's are one (the interface's); size: a JDK name; sync(): no
    # method takes no arguments
    assert calls == {
        ("external_symbol java:java.util.List.forEach", IR, 0.9),
        ("Api.java/Api#sync().", "name_unique", 0.4),
    }
    assert stats.calls.guessed == 1


def test_an_interface_injects_its_one_bean_or_the_primary_of_several() -> None:
    def bean(name: str, iface: str, *marks: str) -> str:
        lines = "".join(f"@{m}\n" for m in ("Service", *marks))
        return f"package p;\n{lines}public class {name} implements {iface} {{\n}}\n"

    files = {
        "Mail.java": "package p;\npublic interface Mail {\n}\n",
        "Sms.java": "package p;\npublic interface Sms {\n}\n",
        "Push.java": "package p;\npublic interface Push {\n}\n",
        "SmtpMail.java": bean("SmtpMail", "Mail"),
        "FastSms.java": bean("FastSms", "Sms"),
        "CheapSms.java": bean("CheapSms", "Sms", "Primary"),
        "ApnsPush.java": bean("ApnsPush", "Push"),
        "FcmPush.java": bean("FcmPush", "Push"),
        "Notifier.java": (
            "package p;\nimport lombok.RequiredArgsConstructor;\n@Service\n"
            "@RequiredArgsConstructor\npublic class Notifier {\n    private final Mail mail;\n"
            "    private final Sms sms;\n    private final Push push;\n}\n"
        ),
    }
    _, edges, stats = _resolve(files, root="src")
    injects = {
        (e.dst.removeprefix("java src/"), e.provenance.value, e.attrs.get("implements"))
        for e in edges
        if e.type is EdgeType.INJECTS
    }
    assert injects == {
        ("Mail.java/Mail#", IR, None),
        ("SmtpMail.java/SmtpMail#", RH, "java src/Mail.java/Mail#"),
        ("Sms.java/Sms#", IR, None),
        ("CheapSms.java/CheapSms#", RH, "java src/Sms.java/Sms#"),  # @Primary
        ("Push.java/Push#", IR, None),  # two beans, none primary: Spring needs a @Qualifier
    }
    assert stats.di.implementations == 2


# ---------------------------------------------------------------------- the graph stage


def _graph_of(project: Project, nodes: list[GraphNode], edges: list[GraphEdge]) -> GraphSnapshot:
    held: dict[str, Node] = {}
    for path, found in project.definitions.items():
        for d in found:
            held[d.symbol_key] = Node(
                d.symbol_key, d.kind, d.name, d.qualified_name, path, d.start_line, d.end_line,
                "java", lane(d.kind, path, None),
            )  # fmt: skip
    for n in nodes:
        held.setdefault(
            n.key,
            Node(
                n.key, n.kind.value, n.name, n.qualified_name or n.name, n.path, n.start_line,
                n.end_line, n.lang, lane(n.kind.value, n.path, None),
            ),
        )  # fmt: skip
    order = sorted(held)
    at = {key: i for i, key in enumerate(order)}
    return GraphSnapshot(
        [held[key] for key in order],
        [
            (
                at[e.src],
                at[e.dst],
                Edge(
                    e.type,
                    e.confidence if e.confidence is not None else confidence_of(e.provenance),
                    e.provenance,
                    e.evidence_path,
                    e.evidence_line,
                ),
            )
            for e in edges
            if e.src in at and e.dst in at
        ],
    )


def test_the_graph_stage_resolves_java_end_to_end(tmp_path: Path) -> None:
    """The pool's work (parse and extract, from the working copy) and the stage's resolution
    over the registered resolvers, as the graph stage runs them; then a trace from the
    controller's handler reaches the implementation through the interface."""
    assert resolver_for("java") == "java"
    assert RESOLVERS["java"] is java
    for name, content in FILES.items():
        (tmp_path / ROOT / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / ROOT / name).write_text(content)
    paths = sorted(f"{ROOT}/{name}" for name in FILES)
    facts: dict[str, dict[str, object]] = {}
    definitions: dict[str, tuple[Definition, ...]] = {}
    for path in paths:
        result = extract_request(GraphRequest(str(tmp_path), path, "java", "java"))
        assert result.failure is None, result.failure
        facts.setdefault(result.resolver, {})[path] = result.facts
        parsed = parse_file(path, (tmp_path / path).read_bytes())
        definitions[path] = tuple(
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in extract_symbols(parsed, "java")
        )  # fmt: skip
    project = Project(frozenset(paths), {}, definitions)
    build = resolve_graph(project, facts, clock=lambda: 0.0)
    CODE.check(build.nodes, build.edges)
    details = cast("dict[str, Any]", build.details["java"])
    assert (details["files"], details["seconds"]) == (12, 0.0)
    assert details["imports"] == {
        "internal": 13, "resolved": 13, "external": 13, "unresolved": 0,
        "unresolved_examples": [],
    }  # fmt: skip
    assert set(details) >= {"calls", "code", "di"}
    assert details["di"]["injects"] == 4
    defined = {d.symbol_key for found in definitions.values() for d in found}
    # Every edge starts at a definition the symbols stage saved; attrs go on definitions too.
    assert {e.src for e in build.edges} <= defined
    assert set(build.attrs) <= defined
    assert build.tables[_key("web/CartController.java/")]["CartService"] == _key(SERVICE)
    graph = _graph_of(project, build.nodes, build.edges)
    result = trace_feature(
        graph,
        _key("web/CartController.java/CartController#add()."),
        to=_key(f"{IMPL}addItem()."),
    )
    best = result.paths[0]
    assert best.reaches_goal
    assert [hop.node.symbol_key for hop in best.hops] == [
        _key("web/CartController.java/CartController#add()."),
        _key(f"{SERVICE}addItem()."),
        _key(f"{IMPL}addItem()."),
    ]
    assert best.hops[-1].edge_type is EdgeType.IMPLEMENTS  # the override step
