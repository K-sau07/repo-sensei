"""Ruby: the queries (definitions, requires, calls, strings, references) and the symbols of a
small Rails application (a controller in nested modules, an ActiveRecord model, a concern, a
service with class methods), keyed (a compact class name keyed as if nested), spanned and
described; the lanes of a Rails layout and a file going through parsing, symbols and chunks end
to end."""

from __future__ import annotations

import pytest

from sensei_engine.chunk.code import chunk_code
from sensei_engine.chunk.files import chunk_file
from sensei_engine.graph.snapshot import lane
from sensei_engine.ingest.roles import assign_role
from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult, ParseStatus, parse_file
from sensei_engine.symbols.extract import Symbol, SymbolKind, extract_symbols

CONTROLLER = """require "csv"
require_relative "../../../lib/pricing"

module Api
  module V1
    # Orders, over HTTP.
    class OrdersController < ApplicationController
      include Pagination
      before_action :set_order, only: %i[show update]

      PER_PAGE = 20

      # GET /api/v1/orders/:id
      def show
        render json: OrderSerializer.new(@order).as_json
      end

      def create
        @order = Order.new(order_params)
        if @order.save
          render json: @order, status: :created
        else
          render json: @order.errors, status: :unprocessable_entity
        end
      end

      def update = @order.update!(order_params)

      private

      def set_order
        @order = Order.find(params[:id])
      end

      def order_params
        params.require(:order).permit(:customer_id, :total)
      end
    end
  end
end
"""

MODEL = """# An order a customer placed.
class Order < ApplicationRecord
  include Billable
  belongs_to :customer
  has_many :lines, dependent: :destroy

  STATUSES = %w[open paid closed].freeze

  scope :open, -> { where(status: "open") }

  # Finds an order by its public token.
  def self.find_by_token(token)
    find_by!(token: token)
  end

  class << self
    def recent
      where("created_at > ?", 1.week.ago)
    end
  end

  def total
    lines.sum(&:amount)
  end

  def paid? = status == "paid"
end

module Billable
  def charge!(gateway)
    gateway.charge(total, currency: "EUR")
  end
end

class Api::V2::OrdersController < ApplicationController; end
"""


def _parse(path: str, source: str, kinds: list[QueryKind] | None = None) -> ParseResult:
    result = parse_file(path, source, kinds=kinds)
    expected = (ParseStatus.PARSED, "ruby", 0)
    assert (result.status, result.language, result.error_count) == expected
    return result


def _matches(result: ParseResult, kind: QueryKind, *roles: str) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for match in result.captures[kind]:
        values = [result.text(n) if (n := match.role(r)) is not None else "" for r in roles]
        found.append((match.kind, *values))
    return found


def _symbols(path: str, source: str) -> dict[str, Symbol]:
    symbols = extract_symbols(_parse(path, source), "ruby")
    keys = [s.symbol_key for s in symbols]
    assert len(keys) == len(set(keys))
    assert keys[0] == f"rb {path}/"
    return {s.symbol_key.removeprefix(f"rb {path}/"): s for s in symbols}


@pytest.fixture(scope="module")
def controller() -> dict[str, Symbol]:
    return _symbols("app/controllers/api/v1/orders_controller.rb", CONTROLLER)


@pytest.fixture(scope="module")
def model() -> dict[str, Symbol]:
    return _symbols("app/models/order.rb", MODEL)


# ---------------------------------------------------------------------------- queries


def test_definitions_nested_modules_class_methods_and_constants() -> None:
    result = _parse("app/models/order.rb", MODEL, [QueryKind.DEFS])
    assert _matches(result, QueryKind.DEFS, "owner", "name") == [
        ("definition.class", "", "Order"),
        ("definition.field", "", "STATUSES"),
        ("definition.classmethod", "", "find_by_token"),
        ("definition.classmethod", "", "recent"),
        ("definition.method", "", "total"),
        ("definition.method", "", "paid?"),
        ("definition.mod", "", "Billable"),
        ("definition.method", "", "charge!"),
        ("definition.class", "Api::V2", "OrdersController"),
    ]


def test_requires_and_relative_requires() -> None:
    result = _parse("app/controllers/api/v1/orders_controller.rb", CONTROLLER, [QueryKind.IMPORTS])
    assert _matches(result, QueryKind.IMPORTS, "source") == [
        ("import.require", "csv"),
        ("import.relative", "../../../lib/pricing"),
    ]
    # params.require(:order) is a method call, not a require.
    assert len(result.captures[QueryKind.IMPORTS]) == 2


def test_calls_with_receivers_bare_calls_and_new() -> None:
    result = _parse("app/controllers/api/v1/orders_controller.rb", CONTROLLER, [QueryKind.CALLS])
    found = _matches(result, QueryKind.CALLS, "receiver", "name", "arguments")
    assert found[:6] == [
        ("call", "", "require", '"csv"'),
        ("call", "", "require_relative", '"../../../lib/pricing"'),
        ("call", "", "include", "Pagination"),
        ("call", "", "before_action", ":set_order, only: %i[show update]"),
        ("call", "", "render", "json: OrderSerializer.new(@order).as_json"),
        ("call.method", "OrderSerializer.new(@order)", "as_json", ""),
    ]
    assert ("call.new", "", "OrderSerializer", "(@order)") in found
    assert ("call.new", "", "Order", "(order_params)") in found
    assert ("call.method", "@order", "save", "") in found  # no arguments, no list
    assert ("call.method", "Order", "find", "(params[:id])") in found
    assert ("call.method", "params", "require", "(:order)") in found
    assert not any(m[2] == "new" for m in found)


def test_strings_and_heredocs() -> None:
    result = parse_file(
        "a.rb", 'a = "x"\nsql = <<~SQL\n  select 1\nSQL\n', kinds=[QueryKind.STRINGS]
    )
    assert [m.kind for m in result.captures[QueryKind.STRINGS]] == ["string", "string.heredoc"]


def test_references_tell_methods_constants_and_instance_variables_apart() -> None:
    result = parse_file("a.rb", "x = Order.find(@id)\n", kinds=[QueryKind.REFS])
    assert [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.REFS]] == [
        ("reference.identifier", "x"),
        ("reference.constant", "Order"),
        ("reference.method", "find"),
        ("reference.field", "@id"),
    ]


# ---------------------------------------------------------------------------- symbols


def test_keys_nest_through_modules_and_classes(controller: dict[str, Symbol]) -> None:
    found = {key: (s.kind, s.attrs.get("subkind")) for key, s in controller.items()}
    assert found == {
        "": (SymbolKind.MODULE, None),
        "Api#": (SymbolKind.MODULE, "module"),
        "Api#V1#": (SymbolKind.MODULE, "module"),
        "Api#V1#OrdersController#": (SymbolKind.CLASS, None),
        "Api#V1#OrdersController#PER_PAGE.": (SymbolKind.VARIABLE, "field"),
        "Api#V1#OrdersController#show().": (SymbolKind.METHOD, None),
        "Api#V1#OrdersController#create().": (SymbolKind.METHOD, None),
        "Api#V1#OrdersController#update().": (SymbolKind.METHOD, None),
        "Api#V1#OrdersController#set_order().": (SymbolKind.METHOD, None),
        "Api#V1#OrdersController#order_params().": (SymbolKind.METHOD, None),
    }
    show = controller["Api#V1#OrdersController#show()."]
    assert show.qualified_name == "Api.V1.OrdersController.show"
    path = "app/controllers/api/v1/orders_controller.rb"
    assert show.container_key == f"rb {path}/Api#V1#OrdersController#"


def test_a_compact_class_name_is_keyed_as_if_nested(model: dict[str, Symbol]) -> None:
    compact = model["Api#V2#OrdersController#"]
    assert (compact.kind, compact.qualified_name) == (SymbolKind.CLASS, "Api.V2.OrdersController")
    assert compact.signature == "class Api::V2::OrdersController < ApplicationController"
    assert compact.container_key == "rb app/models/order.rb/"


def test_class_methods_and_names_with_punctuation(model: dict[str, Symbol]) -> None:
    found = {key: s.attrs.get("subkind") for key, s in model.items() if s.kind is SymbolKind.METHOD}
    assert found == {
        "Order#find_by_token().": "classmethod",
        "Order#recent().": "classmethod",  # in class << self
        "Order#total().": None,
        "Order#`paid?`().": None,
        "Billable#`charge!`().": None,
    }


def test_spans_and_signatures(controller: dict[str, Symbol], model: dict[str, Symbol]) -> None:
    spans = {key: (s.start_line, s.end_line) for key, s in controller.items()}
    assert spans["Api#"] == (4, 40)
    assert spans["Api#V1#OrdersController#"] == (7, 38)
    assert spans["Api#V1#OrdersController#show()."] == (14, 16)
    assert controller["Api#V1#OrdersController#"].signature == (
        "class OrdersController < ApplicationController"
    )
    assert controller["Api#V1#OrdersController#show()."].signature == "def show"
    assert controller["Api#V1#OrdersController#update()."].signature == "def update"
    assert controller["Api#V1#OrdersController#PER_PAGE."].signature == "PER_PAGE = 20"
    assert model["Order#find_by_token()."].signature == "def self.find_by_token(token)"
    assert model["Order#`paid?`()."].signature == "def paid?"
    assert model["Billable#"].signature == "module Billable"


def test_comments_above_are_docstrings(
    controller: dict[str, Symbol], model: dict[str, Symbol]
) -> None:
    # A body's first statement: the comment above it stands outside the body.
    assert controller["Api#V1#OrdersController#"].docstring == "Orders, over HTTP."
    assert controller["Api#V1#OrdersController#show()."].docstring == "GET /api/v1/orders/:id"
    assert controller["Api#V1#OrdersController#create()."].docstring is None
    assert model["Order#"].docstring == "An order a customer placed."
    assert model["Order#find_by_token()."].docstring == "Finds an order by its public token."


def test_the_superclass_and_included_modules_are_bases(
    controller: dict[str, Symbol], model: dict[str, Symbol]
) -> None:
    assert controller["Api#V1#OrdersController#"].attrs == {
        "bases": ["ApplicationController", "Pagination"]
    }
    assert model["Order#"].attrs == {"bases": ["ApplicationRecord", "Billable"]}
    assert model["Billable#"].attrs == {"subkind": "module"}


# ---------------------------------------------------------------------------- end to end


def test_a_ruby_file_is_parsed_keyed_and_chunked_by_definition() -> None:
    path = "app/controllers/api/v1/orders_controller.rb"
    steps = "".join(
        f'        Audit.record("step-{i}", @order.id, @order.total, {i})\n' for i in range(14)
    )
    source = CONTROLLER.replace("        if @order.save\n", steps + "        if @order.save\n")
    result = parse_file(path, source.encode())  # the language comes from the path
    assert (result.status, result.language) == (ParseStatus.PARSED, "ruby")
    assert all(result.captures[k] for k in (QueryKind.DEFS, QueryKind.IMPORTS, QueryKind.CALLS))
    symbols = extract_symbols(result, "ruby")
    assert symbols[0].symbol_key == f"rb {path}/"
    chunks = chunk_file(path, "ruby", source)
    keyed = [(c.symbol_key or "").removeprefix(f"rb {path}/") for c in chunks]
    create = chunks[keyed.index("Api#V1#OrdersController#create().")]
    assert create.kind.value == "method"
    assert create.content.lstrip().startswith("def create")
    assert "lang: ruby" in create.header
    # The modules' and the class's closing ends join the definitions before them.
    assert not any(c.content.strip() in ("end", "end\nend") for c in chunks)


# ---------------------------------------------------------------------------- lanes


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/controllers/orders_controller.rb", "api"),
        ("app/controllers/api/v1/orders_controller.rb", "api"),
        ("app/models/order.rb", "db"),
        ("app/models/concerns/billable.rb", "db"),
        ("db/migrate/20260101000000_create_orders.rb", "db"),
        ("app/services/checkout.rb", "svc"),
        ("app/jobs/charge_job.rb", "svc"),
    ],
)
def test_a_rails_layouts_folders_fall_in_their_lanes(path: str, expected: str) -> None:
    role = assign_role(path, "ruby", "class X; end\n", ui_package=False)
    assert lane("class", path, role.value) == expected


def test_six_hundred_nested_modules_chunk_without_recursing_that_deep() -> None:
    """Classes deeper than the chunker splits are kept whole in their parent's chunk."""
    source = "".join(f"module M{i}\ndef f{i}\n" + "x = 1\n" * 8 + "end\n" for i in range(600))
    parsed = parse_file("deep.rb", (source + "end\n" * 600).encode())
    chunks = chunk_code(parsed, "ruby", extract_symbols(parsed, "ruby"))
    assert chunks
    covered = sorted({line for c in chunks for line in range(c.start_line, c.end_line + 1)})
    assert covered[0] == 1
