"""C#: the queries (definitions, usings, calls, attributes, strings, references) and the symbols
of a small ASP.NET Core API (a controller in a block namespace, an EF Core DbContext and its
entities in a file-scoped one, a record, an enum), keyed (namespaces left out), spanned and
described; overloads, nested types, the lanes of an ASP.NET layout and a file going through
parsing, symbols and chunks end to end."""

from __future__ import annotations

import pytest

from sensei_engine.chunk.code import imported_names
from sensei_engine.chunk.files import chunk_file
from sensei_engine.graph.snapshot import lane
from sensei_engine.ingest.roles import assign_role
from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult, ParseStatus, decorated, parse_file
from sensei_engine.symbols.extract import Symbol, SymbolKind, extract_symbols

CONTROLLER = """using System.Threading.Tasks;
using Microsoft.AspNetCore.Mvc;
using Shop.Api.Data;
using static System.Math;
using Dto = Shop.Api.Models.OrderDto;

namespace Shop.Api.Controllers
{
    /// <summary>
    /// Orders, over HTTP.
    /// </summary>
    [ApiController]
    [Route("api/[controller]")]
    public class OrdersController : ControllerBase
    {
        private readonly ShopContext _db;
        private int _served, _failed;

        public OrdersController(ShopContext db) : base()
        {
            _db = db;
        }

        /// <summary>One order.</summary>
        [HttpGet("{id}")]
        public async Task<ActionResult<Dto>> Get(int id)
        {
            var order = await _db.Orders.FindAsync(id);
            return order is null ? NotFound() : Ok(Dto.From(order));
        }

        // Places an order.
        [HttpPost, Authorize(Roles = "clerk")]
        public async Task<IActionResult> Create([FromBody] Dto body)
        {
            var order = new Order { CustomerId = body.CustomerId };
            _db.Orders.Add(order);
            await _db.SaveChangesAsync();
            return CreatedAtAction(nameof(Get), new { id = order.Id }, body);
        }

        public IActionResult Create(int customerId) => Create(new Dto(customerId, 0m)).Result;

        public class Paging
        {
            public int Page { get; init; } = 1;
        }
    }
}
"""

DATA = """using Microsoft.EntityFrameworkCore;

namespace Shop.Api.Data;

public class ShopContext : DbContext
{
    public ShopContext(DbContextOptions<ShopContext> options) : base(options) { }

    public DbSet<Order> Orders => Set<Order>();

    protected override void OnModelCreating(ModelBuilder model)
    {
        model.Entity<Order>().HasIndex(o => o.CustomerId);
    }
}

/* Copyright Acme. Not a description of the class below. */
[Table("orders")]
public class Order : Entity, IAudited
{
    [Key]
    public int Id { get; set; }

    [Column("customer_id")]
    public long CustomerId { get; set; }

    public Status Status { get; set; } = Status.New;
}

public record OrderDto(long CustomerId, decimal Total) : Dto(CustomerId);

public interface IAudited
{
    string Audit();
}

public struct Money
{
    public decimal Amount;
}

public enum Status { New, Paid = 2, Closed }
"""


def _parse(path: str, source: str, kinds: list[QueryKind] | None = None) -> ParseResult:
    result = parse_file(path, source, kinds=kinds)
    expected = (ParseStatus.PARSED, "csharp", 0)
    assert (result.status, result.language, result.error_count) == expected
    return result


def _matches(result: ParseResult, kind: QueryKind, *roles: str) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for match in result.captures[kind]:
        values = [result.text(n) if (n := match.role(r)) is not None else "" for r in roles]
        found.append((match.kind, *values))
    return found


def _symbols(path: str, source: str) -> dict[str, Symbol]:
    symbols = extract_symbols(_parse(path, source), "csharp")
    keys = [s.symbol_key for s in symbols]
    assert len(keys) == len(set(keys))
    assert keys[0] == f"cs {path}/"
    return {s.symbol_key.removeprefix(f"cs {path}/"): s for s in symbols}


@pytest.fixture(scope="module")
def controller() -> dict[str, Symbol]:
    return _symbols("Controllers/OrdersController.cs", CONTROLLER)


@pytest.fixture(scope="module")
def data() -> dict[str, Symbol]:
    return _symbols("Data/ShopContext.cs", DATA)


# ---------------------------------------------------------------------------- queries


def test_definitions_one_per_field_declarator_and_at_any_depth() -> None:
    result = _parse("Controllers/OrdersController.cs", CONTROLLER, [QueryKind.DEFS])
    assert _matches(result, QueryKind.DEFS, "name") == [
        ("definition.module", "Shop.Api.Controllers"),
        ("definition.class", "OrdersController"),
        ("definition.field", "_db"),
        ("definition.field", "_served"),
        ("definition.field", "_failed"),
        ("definition.constructor", "OrdersController"),
        ("definition.method", "Get"),
        ("definition.method", "Create"),
        ("definition.method", "Create"),
        ("definition.class", "Paging"),
        ("definition.property", "Page"),
    ]


def test_usings_static_aliases_and_the_namespace() -> None:
    result = _parse("Controllers/OrdersController.cs", CONTROLLER, [QueryKind.IMPORTS])
    assert _matches(result, QueryKind.IMPORTS, "source", "alias", "name") == [
        ("import.namespace", "System.Threading.Tasks", "", ""),
        ("import.namespace", "Microsoft.AspNetCore.Mvc", "", ""),
        ("import.namespace", "Shop.Api.Data", "", ""),
        ("import.static", "System.Math", "", ""),
        ("import", "Shop.Api.Models.OrderDto", "Dto", ""),
        ("import.package", "", "", "Shop.Api.Controllers"),
    ]
    assert imported_names(result) == ["Dto"]
    scoped = _parse("Data/ShopContext.cs", DATA, [QueryKind.IMPORTS])
    assert _matches(scoped, QueryKind.IMPORTS, "name")[-1] == ("import.package", "Shop.Api.Data")


def test_calls_with_receivers_creations_and_constructor_initializers() -> None:
    result = _parse("Controllers/OrdersController.cs", CONTROLLER, [QueryKind.CALLS])
    found = _matches(result, QueryKind.CALLS, "receiver", "name", "arguments")
    assert found[:5] == [
        ("call.constructor", "", "base", "()"),
        ("call.method", "_db.Orders", "FindAsync", "(id)"),
        ("call", "", "NotFound", "()"),
        ("call", "", "Ok", "(Dto.From(order))"),
        ("call.method", "Dto", "From", "(order)"),
    ]
    assert ("call.new", "", "Order", "") in found  # an object initializer: no argument list
    assert ("call.method", "_db", "SaveChangesAsync", "()") in found
    assert ("call.new", "", "Dto", "(customerId, 0m)") in found
    data = _parse("Data/ShopContext.cs", DATA, [QueryKind.CALLS])
    assert _matches(data, QueryKind.CALLS, "receiver", "name")[:4] == [
        ("call.constructor", "", "base"),
        ("call", "", "Set"),  # Set<Order>(): the name without its type arguments
        ("call.method", "model.Entity<Order>()", "HasIndex"),
        ("call.method", "model", "Entity"),
    ]
    more = parse_file(
        "A.cs",
        "class A { void F() { order?.Total(); this.G(); var l = new List<int>(); } }",
        kinds=[QueryKind.CALLS],
    )
    assert _matches(more, QueryKind.CALLS, "receiver", "name") == [
        ("call.method", "order", "Total"),
        ("call.method", "this", "G"),
        ("call.new", "", "List"),
    ]


def test_attributes_one_each_and_what_they_are_on() -> None:
    result = _parse("Controllers/OrdersController.cs", CONTROLLER, [QueryKind.DECORATORS])
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
        ("ApiController", "", "class_declaration"),
        ("Route", '("api/[controller]")', "class_declaration"),
        ("HttpGet", '("{id}")', "method_declaration"),
        ("HttpPost", "", "method_declaration"),
        ("Authorize", '(Roles = "clerk")', "method_declaration"),
        ("FromBody", "", "parameter"),
    ]


def test_strings_verbatim_raw_and_interpolated() -> None:
    result = parse_file(
        "A.cs",
        'class A { string a = "x"; string b = @"select\n1"; string c = $"{a}!"; }',
        kinds=[QueryKind.STRINGS],
    )
    assert [m.kind for m in result.captures[QueryKind.STRINGS]] == [
        "string",
        "string.verbatim",
        "string.interpolated",
    ]


def test_references_tell_methods_and_members_apart() -> None:
    result = parse_file(
        "A.cs", "class A { void F() { repo.Find(this.Id); } }", kinds=[QueryKind.REFS]
    )
    found = [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.REFS]]
    assert ("reference.method", "Find") in found
    assert ("reference.field", "Id") in found
    assert ("reference.identifier", "repo") in found


# ---------------------------------------------------------------------------- symbols


def test_keys_kinds_and_subkinds_namespaces_left_out_of_keys(
    controller: dict[str, Symbol], data: dict[str, Symbol]
) -> None:
    found = {key: (s.kind, s.attrs.get("subkind")) for key, s in controller.items()}
    assert found == {
        "": (SymbolKind.MODULE, None),
        "`Shop.Api.Controllers`#": (SymbolKind.MODULE, "namespace"),
        "OrdersController#": (SymbolKind.CLASS, None),
        "OrdersController#_db.": (SymbolKind.VARIABLE, "field"),
        "OrdersController#_served.": (SymbolKind.VARIABLE, "field"),
        "OrdersController#_failed.": (SymbolKind.VARIABLE, "field"),
        "OrdersController#OrdersController().": (SymbolKind.METHOD, "constructor"),
        "OrdersController#Get().": (SymbolKind.METHOD, None),
        "OrdersController#Create().": (SymbolKind.METHOD, None),
        "OrdersController#Create().~1": (SymbolKind.METHOD, None),
        "OrdersController#Paging#": (SymbolKind.CLASS, None),
        "OrdersController#Paging#Page.": (SymbolKind.VARIABLE, "property"),
    }
    path = "Controllers/OrdersController.cs"
    assert controller["OrdersController#"].container_key == f"cs {path}/"
    assert controller["OrdersController#"].qualified_name == "OrdersController"
    # A file-scoped namespace keys its types alike.
    assert {
        k: (s.kind, s.attrs.get("subkind"))
        for k, s in data.items()
        if "#" in k and k.count("#") == 1 and k.endswith("#")
    } == {
        "`Shop.Api.Data`#": (SymbolKind.MODULE, "namespace"),
        "ShopContext#": (SymbolKind.CLASS, None),
        "Order#": (SymbolKind.CLASS, None),
        "OrderDto#": (SymbolKind.CLASS, "record"),
        "IAudited#": (SymbolKind.CLASS, "interface"),
        "Money#": (SymbolKind.CLASS, "struct"),
        "Status#": (SymbolKind.CLASS, "enum"),
    }
    assert data["OrderDto#Total."].attrs["subkind"] == "property"
    assert data["Money#Amount."].attrs["subkind"] == "field"
    assert data["Status#Paid."].attrs["subkind"] == "field"


def test_spans_include_attributes_not_the_xml_docs(controller: dict[str, Symbol]) -> None:
    spans = {key: (s.start_line, s.end_line) for key, s in controller.items()}
    assert spans["`Shop.Api.Controllers`#"] == (7, 49)
    assert spans["OrdersController#"] == (12, 48)
    assert spans["OrdersController#_db."] == (16, 16)
    assert spans["OrdersController#Get()."] == (25, 30)
    served = controller["OrdersController#_served."]
    assert (served.start_line, served.start_col, served.end_col) == (17, 20, 27)


def test_signatures_stop_at_the_body_without_attributes(
    controller: dict[str, Symbol], data: dict[str, Symbol]
) -> None:
    signatures = {key: s.signature for key, s in controller.items()}
    assert signatures["OrdersController#"] == "public class OrdersController : ControllerBase"
    assert signatures["OrdersController#_db."] == "private readonly ShopContext _db"
    assert signatures["OrdersController#_failed."] == "private int _failed"
    assert signatures["OrdersController#OrdersController()."] == (
        "public OrdersController(ShopContext db) : base()"
    )
    assert signatures["OrdersController#Get()."] == (
        "public async Task<ActionResult<Dto>> Get(int id)"
    )
    assert signatures["OrdersController#Create()."] == (
        "public async Task<IActionResult> Create([FromBody] Dto body)"
    )
    assert (
        signatures["OrdersController#Create().~1"] == "public IActionResult Create(int customerId)"
    )
    assert signatures["OrdersController#Paging#Page."] == "public int Page {get; init;} = 1"
    assert data["Order#CustomerId."].signature == "public long CustomerId {get; set;}"
    assert data["OrderDto#"].signature == (
        "public record OrderDto(long CustomerId, decimal Total) : Dto(CustomerId)"
    )
    assert data["Status#Paid."].signature == "Paid = 2"


def test_xml_docs_and_line_comments_are_docstrings_a_plain_block_comment_isnt(
    controller: dict[str, Symbol], data: dict[str, Symbol]
) -> None:
    assert controller["OrdersController#"].docstring == "Orders, over HTTP."
    assert controller["OrdersController#Get()."].docstring == "One order."
    assert controller["OrdersController#Create()."].docstring == "Places an order."
    assert controller["OrdersController#Create().~1"].docstring is None
    assert data["Order#"].docstring is None  # the licence above it


def test_attributes_are_decorators_and_async_is_kept(
    controller: dict[str, Symbol], data: dict[str, Symbol]
) -> None:
    assert controller["OrdersController#"].attrs["decorators"] == [
        "[ApiController]",
        '[Route("api/[controller]")]',
    ]
    assert controller["OrdersController#Create()."].attrs == {
        "decorators": ["[HttpPost]", '[Authorize(Roles = "clerk")]'],
        "async": True,
    }
    assert data["Order#"].attrs["decorators"] == ['[Table("orders")]']
    assert data["Order#Id."].attrs["decorators"] == ["[Key]"]
    assert data["Order#CustomerId."].attrs["decorators"] == ['[Column("customer_id")]']


def test_base_lists_are_bases(controller: dict[str, Symbol], data: dict[str, Symbol]) -> None:
    assert controller["OrdersController#"].attrs["bases"] == ["ControllerBase"]
    assert data["ShopContext#"].attrs == {"bases": ["DbContext"]}
    assert data["Order#"].attrs["bases"] == ["Entity", "IAudited"]
    assert data["OrderDto#"].attrs["bases"] == ["Dto"]  # without its arguments


# ---------------------------------------------------------------------------- end to end


def test_a_csharp_file_is_parsed_keyed_and_chunked_by_definition() -> None:
    path = "Controllers/OrdersController.cs"
    steps = "".join(
        f'            _audit.Record("step-{i}", body.CustomerId, body.Total, {i});\n'
        for i in range(14)
    )
    source = CONTROLLER.replace(
        "            _db.Orders.Add(order);\n", steps + "            _db.Orders.Add(order);\n"
    )
    result = parse_file(path, source.encode())  # the language comes from the path
    assert (result.status, result.language) == (ParseStatus.PARSED, "csharp")
    assert all(
        result.captures[kind]
        for kind in (QueryKind.DEFS, QueryKind.IMPORTS, QueryKind.CALLS, QueryKind.DECORATORS)
    )
    symbols = extract_symbols(result, "csharp")
    assert symbols[0].symbol_key == f"cs {path}/"
    chunks = chunk_file(path, "csharp", source)
    keyed = [(c.symbol_key or "").removeprefix(f"cs {path}/") for c in chunks]
    # The namespace block is no unit of its own: its usings and opening lines are other code,
    # the class inside it a class with its header and methods.
    assert keyed[:2] == ["", "OrdersController#"]
    assert "OrdersController#Create()." in keyed
    create = chunks[keyed.index("OrdersController#Create().")]
    assert create.kind.value == "method"
    assert create.content.lstrip().startswith("// Places an order.")
    assert "lang: csharp" in create.header


# ---------------------------------------------------------------------------- lanes


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/Shop.Api/Controllers/OrdersController.cs", "api"),
        ("src/Shop.Api/Models/OrderDto.cs", "db"),
        ("src/Shop.Api/Entities/Order.cs", "db"),
        ("src/Shop.Api/Services/Checkout.cs", "svc"),
        ("src/Shop.Api/Program.cs", "svc"),
    ],
)
def test_an_aspnet_layouts_folders_fall_in_their_lanes(path: str, expected: str) -> None:
    role = assign_role(path, "csharp", "namespace Shop.Api;\n", ui_package=False)
    assert lane("class", path, role.value) == expected


def test_entities_is_the_data_layer_only_in_csharp() -> None:
    role = assign_role("src/entities/order.ts", "typescript", "", ui_package=False)
    assert role.value == "service"
