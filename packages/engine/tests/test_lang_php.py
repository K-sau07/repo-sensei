"""PHP: the queries (definitions, use declarations, calls, attributes, strings, references) and
the symbols of a small Laravel application (a controller with promoted properties, an Eloquent
model, an interface, a trait, a backed enum), keyed (namespaces left out), spanned and
described; a template's PHP among its HTML, the lanes of a Laravel layout and a file going
through parsing, symbols and chunks end to end."""

from __future__ import annotations

import pytest

from sensei_engine.chunk.code import imported_names
from sensei_engine.chunk.files import chunk_file
from sensei_engine.graph.snapshot import lane
from sensei_engine.ingest.roles import assign_role
from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult, ParseStatus, decorated, parse_file
from sensei_engine.symbols.extract import Symbol, SymbolKind, extract_symbols

CONTROLLER = """<?php

declare(strict_types=1);

namespace App\\Http\\Controllers;

use App\\Models\\Order;
use App\\Services\\{OrderService, PaymentService as Payments};
use Illuminate\\Http\\JsonResponse;
use Illuminate\\Http\\Request as HttpRequest;
use function App\\Support\\money;

/**
 * Orders, over HTTP.
 */
#[Middleware('auth')]
final class OrderController extends Controller implements HasMiddleware
{
    use AuthorizesRequests, ValidatesRequests;

    public const PER_PAGE = 20;

    protected array $casts = [], $hidden = ['token'];

    public function __construct(
        private readonly OrderService $orders,
        #[Inject] protected Payments $payments,
    ) {
        parent::__construct();
    }

    /** One order. */
    #[Get('/orders/{id}')]
    public function show(int $id): JsonResponse
    {
        $order = Order::findOrFail($id);
        $this->authorize('view', $order);
        return response()->json($this->orders->present($order));
    }

    // Places an order.
    #[Post('/orders'), Validate(OrderRequest::class)]
    public function store(HttpRequest $request): JsonResponse
    {
        $order = new Order($request->validated());
        $this->payments->charge($order, money($order->total));
        $order?->save();
        return new JsonResponse($order, 201);
    }

    abstract protected function guard(): string;
}
"""

MODEL = """<?php

namespace App\\Models;

use Illuminate\\Database\\Eloquent\\Model;

/* Copyright Acme. Not a description of the class below. */
class Order extends Model
{
    use HasFactory, SoftDeletes;

    protected $table = 'orders';

    protected $fillable = ['customer_id', 'total', 'status'];

    public function customer(): BelongsTo
    {
        return $this->belongsTo(Customer::class);
    }

    public static function open(): Builder
    {
        return static::query()->where('status', Status::Open->value);
    }
}

interface Payable
{
    public function pay(int $cents): bool;
}

trait HasTotals
{
    public function total(): int
    {
        return array_sum(array_column($this->lines, 'cents'));
    }
}

/** Where an order is. */
enum Status: string
{
    case Open = 'open';
    case Paid = 'paid';

    public function label(): string
    {
        return ucfirst($this->value);
    }
}

function money(int $cents): string
{
    return sprintf('%.2f', $cents / 100);
}

const CURRENCY = 'EUR';
"""

TEMPLATE = """<html>
<body>
<?php function row(array $order): string { return "<tr>{$order['id']}</tr>"; } ?>
<table><?= row($order) ?></table>
</body>
</html>
"""


def _parse(path: str, source: str, kinds: list[QueryKind] | None = None) -> ParseResult:
    result = parse_file(path, source, kinds=kinds)
    assert (result.status, result.language, result.error_count) == (ParseStatus.PARSED, "php", 0)
    return result


def _matches(result: ParseResult, kind: QueryKind, *roles: str) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for match in result.captures[kind]:
        values = [result.text(n) if (n := match.role(r)) is not None else "" for r in roles]
        found.append((match.kind, *values))
    return found


def _symbols(path: str, source: str) -> dict[str, Symbol]:
    symbols = extract_symbols(_parse(path, source), "php")
    keys = [s.symbol_key for s in symbols]
    assert len(keys) == len(set(keys))
    assert keys[0] == f"php {path}/"
    return {s.symbol_key.removeprefix(f"php {path}/"): s for s in symbols}


@pytest.fixture(scope="module")
def controller() -> dict[str, Symbol]:
    return _symbols("app/Http/Controllers/OrderController.php", CONTROLLER)


@pytest.fixture(scope="module")
def model() -> dict[str, Symbol]:
    return _symbols("app/Models/Order.php", MODEL)


# ---------------------------------------------------------------------------- queries


def test_definitions_with_promoted_properties_constants_and_cases() -> None:
    result = _parse("app/Models/Order.php", MODEL, [QueryKind.DEFS])
    assert _matches(result, QueryKind.DEFS, "name") == [
        ("definition.module", "App\\Models"),
        ("definition.class", "Order"),
        ("definition.field", "table"),
        ("definition.field", "fillable"),
        ("definition.method", "customer"),
        ("definition.method", "open"),
        ("definition.interface", "Payable"),
        ("definition.method", "pay"),
        ("definition.trait", "HasTotals"),
        ("definition.method", "total"),
        ("definition.enum", "Status"),
        ("definition.field", "Open"),
        ("definition.field", "Paid"),
        ("definition.method", "label"),
        ("definition.function", "money"),
        ("definition.variable", "CURRENCY"),
    ]
    controller = _parse("app/Http/Controllers/OrderController.php", CONTROLLER, [QueryKind.DEFS])
    names = _matches(controller, QueryKind.DEFS, "name")
    assert names[2:8] == [
        ("definition.field", "PER_PAGE"),
        ("definition.field", "casts"),
        ("definition.field", "hidden"),
        ("definition.constructor", "__construct"),
        ("definition.field", "orders"),
        ("definition.field", "payments"),
    ]


def test_use_declarations_groups_aliases_and_the_namespace() -> None:
    result = _parse("app/Http/Controllers/OrderController.php", CONTROLLER, [QueryKind.IMPORTS])
    assert _matches(result, QueryKind.IMPORTS, "source", "name", "alias") == [
        ("import.package", "", "App\\Http\\Controllers", ""),
        ("import", "App\\Models", "Order", ""),
        ("import", "App\\Services", "OrderService", ""),
        ("import", "App\\Services", "PaymentService", "Payments"),
        ("import", "Illuminate\\Http", "JsonResponse", ""),
        ("import", "Illuminate\\Http", "Request", "HttpRequest"),
        ("import.function", "App\\Support", "money", ""),
    ]
    assert imported_names(result) == [
        "Order",
        "OrderService",
        "Payments",
        "JsonResponse",
        "HttpRequest",
        "money",
    ]
    required = parse_file(
        "a.php",
        "<?php\nrequire_once 'vendor/autoload.php';\nuse Exception;\n",
        kinds=[QueryKind.IMPORTS],
    )
    assert _matches(required, QueryKind.IMPORTS, "source", "name") == [
        ("import.require", "'vendor/autoload.php'", ""),
        ("import", "", "Exception"),
    ]


def test_calls_with_receivers_static_calls_and_creations() -> None:
    result = _parse("app/Http/Controllers/OrderController.php", CONTROLLER, [QueryKind.CALLS])
    found = _matches(result, QueryKind.CALLS, "receiver", "name", "arguments")
    assert found == [
        ("call.method", "parent", "__construct", "()"),
        ("call.method", "Order", "findOrFail", "($id)"),
        ("call.method", "$this", "authorize", "('view', $order)"),
        ("call.method", "response()", "json", "($this->orders->present($order))"),
        ("call", "", "response", "()"),
        ("call.method", "$this->orders", "present", "($order)"),
        ("call.new", "", "Order", "($request->validated())"),
        ("call.method", "$request", "validated", "()"),
        ("call.method", "$this->payments", "charge", "($order, money($order->total))"),
        ("call", "", "money", "($order->total)"),
        ("call.method", "$order", "save", "()"),
        ("call.new", "", "JsonResponse", "($order, 201)"),
    ]


def test_attributes_one_each_and_what_they_are_on() -> None:
    result = _parse("app/Http/Controllers/OrderController.php", CONTROLLER, [QueryKind.DECORATORS])
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
        ("Middleware", "('auth')", "class_declaration"),
        ("Inject", "", "property_promotion_parameter"),
        ("Get", "('/orders/{id}')", "method_declaration"),
        ("Post", "('/orders')", "method_declaration"),
        ("Validate", "(OrderRequest::class)", "method_declaration"),
    ]


def test_strings_interpolated_and_heredocs() -> None:
    result = parse_file(
        "a.php",
        "<?php\n$a = 'x';\n$b = \"{$a}!\";\n$c = <<<SQL\nselect 1\nSQL;\n",
        kinds=[QueryKind.STRINGS],
    )
    assert [m.kind for m in result.captures[QueryKind.STRINGS]] == [
        "string",
        "string.interpolated",
        "string.heredoc",
    ]


def test_references_tell_methods_properties_variables_and_types_apart() -> None:
    result = parse_file(
        "a.php", "<?php\nfunction f(Order $o) { $o->repo->find($o->id); }\n", kinds=[QueryKind.REFS]
    )
    found = [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.REFS]]
    assert found == [
        ("reference.identifier", "f"),
        ("reference.type", "Order"),
        ("reference.variable", "o"),
        ("reference.variable", "o"),
        ("reference.field", "repo"),
        ("reference.method", "find"),
        ("reference.variable", "o"),
        ("reference.field", "id"),
    ]


def test_a_templates_php_parses_among_its_html() -> None:
    result = _parse("resources/views/orders.php", TEMPLATE)
    symbols = extract_symbols(result, "php")
    assert [s.symbol_key for s in symbols] == [
        "php resources/views/orders.php/",
        "php resources/views/orders.php/row().",
    ]
    assert _matches(result, QueryKind.CALLS, "name") == [("call", "row")]


# ---------------------------------------------------------------------------- symbols


def test_keys_kinds_subkinds_namespace_left_out(model: dict[str, Symbol]) -> None:
    found = {key: (s.kind, s.attrs.get("subkind"), s.qualified_name) for key, s in model.items()}
    assert found == {
        "": (SymbolKind.MODULE, None, "app.Models.Order"),
        "`App\\Models`#": (SymbolKind.MODULE, "namespace", "App\\Models"),
        "Order#": (SymbolKind.CLASS, None, "Order"),
        "Order#table.": (SymbolKind.VARIABLE, "field", "Order.table"),
        "Order#fillable.": (SymbolKind.VARIABLE, "field", "Order.fillable"),
        "Order#customer().": (SymbolKind.METHOD, None, "Order.customer"),
        "Order#open().": (SymbolKind.METHOD, None, "Order.open"),
        "Payable#": (SymbolKind.CLASS, "interface", "Payable"),
        "Payable#pay().": (SymbolKind.METHOD, None, "Payable.pay"),
        "HasTotals#": (SymbolKind.CLASS, "trait", "HasTotals"),
        "HasTotals#total().": (SymbolKind.METHOD, None, "HasTotals.total"),
        "Status#": (SymbolKind.CLASS, "enum", "Status"),
        "Status#Open.": (SymbolKind.VARIABLE, "field", "Status.Open"),
        "Status#Paid.": (SymbolKind.VARIABLE, "field", "Status.Paid"),
        "Status#label().": (SymbolKind.METHOD, None, "Status.label"),
        "money().": (SymbolKind.FUNCTION, None, "money"),
        "CURRENCY.": (SymbolKind.VARIABLE, None, "CURRENCY"),
    }


def test_promoted_properties_belong_to_the_class(controller: dict[str, Symbol]) -> None:
    path = "app/Http/Controllers/OrderController.php"
    orders = controller["OrderController#orders."]
    assert orders.container_key == f"php {path}/OrderController#"
    assert orders.attrs == {"subkind": "field"}
    assert orders.signature == "private readonly OrderService $orders"
    payments = controller["OrderController#payments."]
    assert payments.attrs == {"subkind": "field", "decorators": ["#[Inject]"]}
    assert payments.signature == "protected Payments $payments"
    assert controller["OrderController#__construct()."].attrs == {"subkind": "constructor"}


def test_spans_include_attributes_not_the_phpdoc(controller: dict[str, Symbol]) -> None:
    spans = {key: (s.start_line, s.end_line) for key, s in controller.items()}
    assert spans["`App\\Http\\Controllers`#"] == (5, 5)
    assert spans["OrderController#"] == (16, 52)
    assert spans["OrderController#PER_PAGE."] == (21, 21)
    assert spans["OrderController#show()."] == (33, 39)
    casts = controller["OrderController#casts."]
    assert (casts.start_line, casts.start_col, casts.end_col) == (23, 20, 31)


def test_signatures_stop_at_the_body_without_attributes(
    controller: dict[str, Symbol], model: dict[str, Symbol]
) -> None:
    assert controller["OrderController#"].signature == (
        "final class OrderController extends Controller implements HasMiddleware"
    )
    assert controller["OrderController#show()."].signature == (
        "public function show(int $id): JsonResponse"
    )
    assert controller["OrderController#guard()."].signature == (
        "abstract protected function guard(): string"
    )
    assert controller["OrderController#PER_PAGE."].signature == "public const PER_PAGE = 20"
    assert controller["OrderController#hidden."].signature == "protected array $hidden = ['token']"
    assert model["Order#table."].signature == "protected $table = 'orders'"
    assert model["Status#"].signature == "enum Status: string"
    assert model["Status#Paid."].signature == "case Paid = 'paid'"
    assert model["CURRENCY."].signature == "const CURRENCY = 'EUR'"


def test_phpdoc_and_line_comments_are_docstrings_a_plain_block_comment_isnt(
    controller: dict[str, Symbol], model: dict[str, Symbol]
) -> None:
    assert controller["OrderController#"].docstring == "Orders, over HTTP."
    assert controller["OrderController#show()."].docstring == "One order."
    assert controller["OrderController#store()."].docstring == "Places an order."
    assert model["Order#"].docstring is None  # the licence above it
    assert model["Status#"].docstring == "Where an order is."


def test_attributes_are_decorators(controller: dict[str, Symbol]) -> None:
    assert controller["OrderController#"].attrs["decorators"] == ["#[Middleware('auth')]"]
    assert controller["OrderController#store()."].attrs["decorators"] == [
        "#[Post('/orders')]",
        "#[Validate(OrderRequest::class)]",
    ]


def test_superclass_interfaces_and_traits_are_bases(
    controller: dict[str, Symbol], model: dict[str, Symbol]
) -> None:
    assert controller["OrderController#"].attrs["bases"] == [
        "Controller",
        "HasMiddleware",
        "AuthorizesRequests",
        "ValidatesRequests",
    ]
    assert model["Order#"].attrs == {"bases": ["Model", "HasFactory", "SoftDeletes"]}


# ---------------------------------------------------------------------------- end to end


def test_a_php_file_is_parsed_keyed_and_chunked_by_definition() -> None:
    path = "app/Http/Controllers/OrderController.php"
    steps = "".join(
        f"        $this->audit->record('step-{i}', $order->id, $order->total, {i});\n"
        for i in range(14)
    )
    source = CONTROLLER.replace("        $order?->save();\n", steps + "        $order?->save();\n")
    result = parse_file(path, source.encode())  # the language comes from the path
    assert (result.status, result.language) == (ParseStatus.PARSED, "php")
    assert all(
        result.captures[kind]
        for kind in (QueryKind.DEFS, QueryKind.IMPORTS, QueryKind.CALLS, QueryKind.DECORATORS)
    )
    symbols = extract_symbols(result, "php")
    assert symbols[0].symbol_key == f"php {path}/"
    chunks = chunk_file(path, "php", source)
    keyed = [(c.symbol_key or "").removeprefix(f"php {path}/") for c in chunks]
    assert keyed[:2] == ["", "OrderController#"]
    store = chunks[keyed.index("OrderController#store().")]
    assert store.kind.value == "method"
    assert store.content.lstrip().startswith("// Places an order.")
    assert "lang: php" in store.header


# ---------------------------------------------------------------------------- lanes


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/Http/Controllers/OrderController.php", "api"),
        ("app/Http/Controllers/Api/V1/OrderController.php", "api"),
        ("routes/api.php", "api"),
        ("app/Models/Order.php", "db"),
        ("database/migrations/2026_01_01_create_orders_table.php", "db"),
        ("app/Services/Checkout.php", "svc"),
    ],
)
def test_a_laravel_layouts_folders_fall_in_their_lanes(path: str, expected: str) -> None:
    role = assign_role(path, "php", "<?php\n", ui_package=False)
    assert lane("class", path, role.value) == expected
