"""C++: the queries (definitions, includes and usings, calls, strings, references) and the symbols
of a small HTTP router (its header and its source) and a cart defined in one file, keyed (a
namespace left out, a method defined outside its class under it), spanned and described;
templates, constructors, destructors and operators, a declaration and its definition, overloads,
lambdas, a file going through parsing, symbols and chunks end to end, and GoogleTest's files."""

from __future__ import annotations

import pytest

from sensei_engine.chunk.code import imported_names
from sensei_engine.chunk.files import chunk_file
from sensei_engine.ingest.roles import assign_role
from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult, ParseStatus, parse_file
from sensei_engine.symbols.extract import Symbol, SymbolKind, extract_symbols

HEADER = """#pragma once

#include <functional>
#include <memory>
#include <string>
#include <vector>
#include "shop/http.hpp"

namespace shop::http {

using Handler = std::function<Response(const Request&)>;

/// What a route answers to.
enum class Method { Get, Post, Delete };

struct Route {
  Method method;
  std::string path;
  Handler handler;
};

/**
 * Routes requests to the handler registered for their method and path.
 */
class Router : public Component, private NonCopyable {
 public:
  Router();
  explicit Router(std::string prefix);
  ~Router() override;

  Router& add(Method method, const std::string& path, Handler handler);
  Response dispatch(const Request& request) const;
  bool operator==(const Router& other) const;
  static std::unique_ptr<Router> make(std::string prefix);

  template <typename T>
  T& state() { return *static_cast<T*>(state_); }

 private:
  std::string prefix_;
  std::vector<Route> routes_;
  void* state_ = nullptr;
  static int instances;
};

template <typename T>
class Registry {
 public:
  void put(const T& item) { items_.push_back(item); }
  size_t size() const;

 private:
  std::vector<T> items_;
};

}  // namespace shop::http
"""

SOURCE = """#include "shop/router.hpp"

#include <algorithm>
#include <iostream>

using namespace shop::http;
using std::string;
namespace fs = std::filesystem;

int Router::instances = 0;

Router::Router() : prefix_("/") { ++instances; }

Router::Router(std::string prefix) : prefix_(std::move(prefix)) { ++instances; }

Router::~Router() { --instances; }

// Register a handler; routes match in the order they were added.
Router& Router::add(Method method, const std::string& path, Handler handler) {
  routes_.push_back(Route{method, prefix_ + path, std::move(handler)});
  std::sort(routes_.begin(), routes_.end(), by_length);
  return *this;
}

Response Router::dispatch(const Request& request) const {
  auto matches = [&request](const Route& route) {
    return route.method == request.method && route.path == request.path;
  };
  auto found = std::find_if(routes_.begin(), routes_.end(), matches);
  if (found == routes_.end()) {
    return Response::not_found();
  }
  Logger log("router");
  log.info("dispatch " + request.path);
  return found->handler(request);
}

bool Router::operator==(const Router& other) const { return prefix_ == other.prefix_; }

std::unique_ptr<Router> Router::make(std::string prefix) {
  auto router = std::make_unique<Router>(std::move(prefix));
  router->add(Method::Get, "/health", [](const Request&) { return Response::ok("up"); });
  return router;
}

template <typename T>
size_t Registry<T>::size() const {
  return items_.size();
}

void shop::http::serve(Router& router, int port) {
  auto* server = new Server(port);
  server->listen(router);
  std::cout << "listening on " << port << std::endl;
}

static bool by_length(const Route& a, const Route& b) {
  return a.path.size() > b.path.size();
}
"""

CART = """#include <string>

namespace shop {

class Cart {
 public:
  Cart();
  ~Cart();
  void add(int sku, int qty = 1);
  void add(const std::string& code);
  int total() const;
  static int carts;

  class Line {
    int qty() const;
  };

 private:
  int total_ = 0;
};

int Cart::carts = 0;

Cart::Cart() { ++carts; }
Cart::~Cart() { --carts; }

void Cart::add(int sku, int qty) { total_ += sku * qty; }

int Cart::total() const { return total_; }

int Cart::Line::qty() const { return 1; }

void shop::audit(const Cart& cart) {}

}  // namespace shop

void legacy::Ledger::post() {}
"""


def _parse(path: str, source: str, kinds: list[QueryKind] | None = None) -> ParseResult:
    result = parse_file(path, source, kinds=kinds)
    assert (result.status, result.language, result.error_count) == (ParseStatus.PARSED, "cpp", 0)
    return result


def _matches(result: ParseResult, kind: QueryKind, *roles: str) -> list[tuple[str, ...]]:
    """Each match's kind and roles, a role captured several times (a qualified name's scopes)
    joined with ::."""
    found: list[tuple[str, ...]] = []
    for match in result.captures[kind]:
        values = ["::".join(result.text(n) for n in match.roles.get(r, ())) for r in roles]
        found.append((match.kind, *values))
    return found


def _symbols(path: str, source: str) -> dict[str, Symbol]:
    symbols = extract_symbols(_parse(path, source), "cpp")
    keys = [s.symbol_key for s in symbols]
    assert len(keys) == len(set(keys))
    assert keys[0] == f"cpp {path}/"
    return {s.symbol_key.removeprefix(f"cpp {path}/"): s for s in symbols}


@pytest.fixture(scope="module")
def header() -> dict[str, Symbol]:
    return _symbols("include/shop/router.hpp", HEADER)


@pytest.fixture(scope="module")
def router() -> dict[str, Symbol]:
    return _symbols("src/router.cpp", SOURCE)


@pytest.fixture(scope="module")
def cart() -> dict[str, Symbol]:
    return _symbols("src/cart.cpp", CART)


# ---------------------------------------------------------------------------- queries


def test_definitions_namespaces_classes_and_declarations_in_a_class_body() -> None:
    result = _parse("include/shop/router.hpp", HEADER, [QueryKind.DEFS])
    found = [
        (m.kind, result.text(m.role("name") or m.node)) for m in result.captures[QueryKind.DEFS]
    ]
    assert found[:8] == [
        ("definition.module", "shop::http"),
        ("definition.type", "Handler"),
        ("definition.enum", "Method"),
        ("definition.field", "Get"),
        ("definition.field", "Post"),
        ("definition.field", "Delete"),
        ("definition.struct", "Route"),
        ("definition.field", "method"),
    ]
    # In a class body: a constructor or destructor is a declaration, a method (declared or
    # defined) and a data member are field declarations; the symbols tell them apart.
    assert found[10:15] == [
        ("definition.class", "Router"),
        ("definition.method", "Router()"),
        ("definition.method", "Router(std::string prefix)"),
        ("definition.method", "~Router() override"),
        ("definition.field", "& add(Method method, const std::string& path, Handler handler)"),
    ]
    assert ("definition.class", "Registry") in found  # a template's class


def test_includes_usings_namespace_aliases_and_the_files_namespace() -> None:
    header = _parse("include/shop/router.hpp", HEADER, [QueryKind.IMPORTS])
    assert _matches(header, QueryKind.IMPORTS, "source", "name")[-2:] == [
        ("import.local", "shop/http.hpp", ""),
        ("import.package", "", "shop::http"),
    ]
    source = _parse("src/router.cpp", SOURCE, [QueryKind.IMPORTS])
    assert _matches(source, QueryKind.IMPORTS, "source", "alias", "name") == [
        ("import.local", "shop/router.hpp", "", ""),
        ("import.system", "<algorithm>", "", ""),
        ("import.system", "<iostream>", "", ""),
        ("import.namespace", "shop::http", "", ""),  # a directive: not an import of http
        ("import", "std", "", "string"),
        ("import", "std::filesystem", "fs", ""),
    ]
    deep = parse_file("a.cpp", "using acme::net::Socket;\n", kinds=[QueryKind.IMPORTS])
    assert _matches(deep, QueryKind.IMPORTS, "source", "name") == [
        ("import", "acme::net", "Socket")
    ]


def test_imported_names_are_using_declarations_and_aliases() -> None:
    assert imported_names(_parse("src/router.cpp", SOURCE)) == ["string", "fs"]


def test_calls_on_objects_pointers_and_this() -> None:
    result = _parse("src/router.cpp", SOURCE, [QueryKind.CALLS])
    found = _matches(result, QueryKind.CALLS, "receiver", "name", "arguments")
    assert (
        "call.method",
        "routes_",
        "push_back",
        "(Route{method, prefix_ + path, std::move(handler)})",
    ) in found
    assert ("call.method", "log", "info", '("dispatch " + request.path)') in found
    assert ("call.method", "found", "handler", "(request)") in found  # found->handler(request)
    assert ("call.method", "server", "listen", "(router)") in found
    this = parse_file(
        "a.cpp",
        "void A::f() { this->run(); obj.get<int>(1); obj.template as<T>(); p->~T(); }\n",
        kinds=[QueryKind.CALLS],
    )
    assert _matches(this, QueryKind.CALLS, "receiver", "name") == [
        ("call.method", "this", "run"),
        ("call.method", "obj", "get"),
        ("call.method", "obj", "as"),
        ("call.method", "p", "~T"),
    ]


def test_qualified_calls_capture_each_scope_and_template_calls_their_name() -> None:
    result = _parse("src/router.cpp", SOURCE, [QueryKind.CALLS])
    found = _matches(result, QueryKind.CALLS, "receiver", "name")
    assert ("call.method", "std", "sort") in found
    assert ("call.method", "Response", "not_found") in found  # a static method
    source = "void f() { std::chrono::steady_clock::now(); ::reset(); twice<int>(2); }\n"
    calls = parse_file("a.cpp", source, kinds=[QueryKind.CALLS])
    assert _matches(calls, QueryKind.CALLS, "receiver", "name") == [
        ("call.method", "std::chrono::steady_clock", "now"),
        ("call", "", "reset"),
        ("call", "", "twice"),
    ]
    first = calls.captures[QueryKind.CALLS][0]
    assert [calls.text(n) for n in first.roles["receiver"]] == ["std", "chrono", "steady_clock"]
    cast = parse_file("a.cpp", "void f() { g(static_cast<int>(x)); }\n", kinds=[QueryKind.CALLS])
    assert _matches(cast, QueryKind.CALLS, "name") == [("call", "g")]


def test_constructions_new_make_unique_braces_and_declarations() -> None:
    result = _parse("src/router.cpp", SOURCE, [QueryKind.CALLS])
    found = _matches(result, QueryKind.CALLS, "receiver", "name", "arguments")
    news = [f for f in found if f[0] == "call.new"]
    assert news == [
        ("call.new", "", "Route", "{method, prefix_ + path, std::move(handler)}"),
        ("call.new", "", "Logger", '("router")'),  # Logger log("router");
        ("call.new", "", "Router", "(std::move(prefix))"),  # std::make_unique<Router>(...)
        ("call.new", "", "Server", "(port)"),  # new Server(port)
    ]
    # std::make_unique is the construction, not a call of its own.
    assert not any(f[2] == "make_unique" for f in found)
    source = (
        "void f() { auto a = new ns::Box<int>{1}; auto b = std::make_shared<ns::Item>(); "
        'std::string s("x"); auto c = new Order[4]; }\n'
    )
    more = parse_file("a.cpp", source, kinds=[QueryKind.CALLS])
    assert _matches(more, QueryKind.CALLS, "receiver", "name", "arguments") == [
        ("call.new", "ns", "Box", "{1}"),
        ("call.new", "", "ns::Item", "()"),
        ("call.new", "std", "string", '("x")'),
        ("call.new", "", "Order", ""),
    ]


def test_raw_strings_are_told_apart() -> None:
    source = 'const char* q = R"sql(SELECT 1)sql";\nconst char* p = "/orders";\n'
    result = parse_file("a.cpp", source, kinds=[QueryKind.STRINGS])
    assert [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.STRINGS]] == [
        ("string.raw", 'R"sql(SELECT 1)sql"'),
        ("string", '"/orders"'),
    ]


def test_references_tell_methods_fields_scopes_and_types_apart() -> None:
    result = parse_file("a.cpp", "void f(Box* b) { std::move(b->items); b->run(); }\n")
    assert [(m.kind, result.text(m.node)) for m in result.captures[QueryKind.REFS]] == [
        ("reference.identifier", "f"),
        ("reference.type", "Box"),
        ("reference.identifier", "b"),
        ("reference.scope", "std"),
        ("reference.identifier", "move"),
        ("reference.identifier", "b"),
        ("reference.field", "items"),
        ("reference.identifier", "b"),
        ("reference.method", "run"),
    ]


# ---------------------------------------------------------------------------- symbols


def test_keys_kinds_and_subkinds_the_namespace_left_out_of_keys(
    header: dict[str, Symbol],
) -> None:
    found = {key: (s.kind, s.attrs.get("subkind")) for key, s in header.items()}
    assert found == {
        "": (SymbolKind.MODULE, None),
        "`shop::http`#": (SymbolKind.MODULE, "namespace"),
        "Handler#": (SymbolKind.CLASS, "type"),
        "Method#": (SymbolKind.CLASS, "enum"),
        "Method#Get.": (SymbolKind.VARIABLE, "field"),
        "Method#Post.": (SymbolKind.VARIABLE, "field"),
        "Method#Delete.": (SymbolKind.VARIABLE, "field"),
        "Route#": (SymbolKind.CLASS, "struct"),
        "Route#method.": (SymbolKind.VARIABLE, "field"),
        "Route#path.": (SymbolKind.VARIABLE, "field"),
        "Route#handler.": (SymbolKind.VARIABLE, "field"),
        "Router#": (SymbolKind.CLASS, None),
        "Router#Router().": (SymbolKind.METHOD, "constructor"),
        "Router#Router().~1": (SymbolKind.METHOD, "constructor"),  # an overload
        "Router#`~Router`().": (SymbolKind.METHOD, "destructor"),
        "Router#add().": (SymbolKind.METHOD, "signature"),  # declared, defined elsewhere
        "Router#dispatch().": (SymbolKind.METHOD, "signature"),
        "Router#`operator==`().": (SymbolKind.METHOD, "signature"),
        "Router#make().": (SymbolKind.METHOD, "signature"),
        "Router#state().": (SymbolKind.METHOD, None),  # a template, defined in the class
        "Router#prefix_.": (SymbolKind.VARIABLE, "field"),
        "Router#routes_.": (SymbolKind.VARIABLE, "field"),
        "Router#state_.": (SymbolKind.VARIABLE, "field"),
        "Router#instances.": (SymbolKind.VARIABLE, "field"),
        "Registry#": (SymbolKind.CLASS, None),
        "Registry#put().": (SymbolKind.METHOD, None),
        "Registry#size().": (SymbolKind.METHOD, "signature"),
        "Registry#items_.": (SymbolKind.VARIABLE, "field"),
    }
    # The namespace holds the file's types without being their container.
    assert header["Router#"].container_key == "cpp include/shop/router.hpp/"


def test_methods_defined_outside_their_class_are_keyed_under_it(
    router: dict[str, Symbol],
) -> None:
    methods = [key for key, s in router.items() if s.kind is SymbolKind.METHOD]
    assert methods == [
        "Router#Router().",
        "Router#Router().~1",
        "Router#`~Router`().",
        "Router#add().",
        "Router#dispatch().",
        "Router#`operator==`().",
        "Router#make().",
        "Registry#size().",  # template <typename T> size_t Registry<T>::size() const
    ]
    path = "src/router.cpp"
    assert router["Router#add()."].container_key == f"cpp {path}/"  # at the top of the file
    assert router["Router#add()."].qualified_name == "Router.add"
    assert router["Router#instances."].attrs == {"subkind": "field"}  # int Router::instances = 0;
    # shop::http::serve: a namespace the file opens is no type to key it under.
    assert router["serve()."].kind is SymbolKind.FUNCTION


def test_a_declaration_and_its_definition_in_one_file_are_one_symbol(
    cart: dict[str, Symbol],
) -> None:
    found = {key: (s.attrs.get("subkind"), s.start_line) for key, s in cart.items()}
    assert found == {
        "": (None, 1),
        "shop#": ("namespace", 3),
        "Cart#": (None, 5),
        # add(const std::string&) is only declared; add(int, int) is defined below, so its
        # declaration (with its default argument) is dropped for the definition.
        "Cart#add().": ("signature", 10),
        "Cart#Line#": (None, 14),
        "Cart#total_.": ("field", 19),
        "Cart#carts.": ("field", 22),  # static int carts; defined as int Cart::carts = 0;
        "Cart#Cart().": ("constructor", 24),
        "Cart#`~Cart`().": ("destructor", 25),
        "Cart#add().~1": (None, 27),
        "Cart#total().": (None, 29),
        "Cart#Line#qty().": (None, 31),  # a nested class's method: both its classes
        "audit().": (None, 33),  # void shop::audit(): shop is the file's namespace
        "legacy#Ledger#post().": (None, 37),  # a namespace the file doesn't know: kept
    }


def test_templates_are_named_without_their_arguments_and_span_their_template(
    header: dict[str, Symbol],
) -> None:
    registry = header["Registry#"]
    assert (registry.start_line, registry.end_line) == (46, 54)  # template <typename T> too
    assert registry.signature == "template <typename T> class Registry"
    specialized = _symbols("a.cpp", "template <>\nclass Registry<int> {\n  int n;\n};\n")
    assert list(specialized) == ["", "Registry#", "Registry#n."]
    assert specialized["Registry#"].signature == "template <> class Registry<int>"
    function = _symbols("a.cpp", "template <>\nvoid swap<int>(int& a, int& b) {}\n")
    assert list(function) == ["", "swap()."]


def test_operators_and_conversions_are_named_by_their_operator() -> None:
    source = (
        "struct Money {\n"
        "  bool operator == (const Money& o) const;\n"
        "  explicit operator bool() const;\n"
        "  int operator()(int x) { return x; }\n"
        "};\n"
        "bool Money::operator==(const Money& o) const { return true; }\n"
        "Money::operator bool() const { return true; }\n"
    )
    money = _symbols("a.cpp", source)
    assert list(money) == [
        "",
        "Money#",
        "Money#`operator()`().",
        "Money#`operator==`().",  # the declaration (spaced) and the definition are one
        "Money#`operator bool`().",
    ]


def test_lambdas_held_in_variables_are_functions(router: dict[str, Symbol]) -> None:
    matches = router["Router#dispatch().matches()."]
    assert (matches.kind, matches.container_key) == (
        SymbolKind.FUNCTION,
        "cpp src/router.cpp/Router#dispatch().",
    )
    assert matches.signature == "auto matches = [&request](const Route& route)"
    assert "Router#dispatch().found." not in router  # a local, no symbol
    top = _symbols("a.cpp", "auto twice = [](int x) { return 2 * x; };\nint limit = 3;\n")
    assert {key: s.kind for key, s in top.items()} == {
        "": SymbolKind.MODULE,
        "twice().": SymbolKind.FUNCTION,
        "limit.": SymbolKind.VARIABLE,
    }


def test_signatures_stop_at_the_body(header: dict[str, Symbol], router: dict[str, Symbol]) -> None:
    assert header["`shop::http`#"].signature == "namespace shop::http"
    assert header["Router#"].signature == "class Router : public Component, private NonCopyable"
    assert header["Method#"].signature == "enum class Method"
    assert header["Handler#"].signature == "using Handler = std::function<Response(const Request&)>"
    assert header["Router#add()."].signature == (
        "Router& add(Method method, const std::string& path, Handler handler)"
    )
    assert header["Router#`~Router`()."].signature == "~Router() override"
    assert header["Router#state()."].signature == "template <typename T> T& state()"
    assert header["Router#state_."].signature == "void* state_ = nullptr"
    assert router["Router#Router().~1"].signature == (
        "Router::Router(std::string prefix) : prefix_(std::move(prefix))"
    )
    assert router["Router#dispatch()."].signature == (
        "Response Router::dispatch(const Request& request) const"
    )


def test_doc_comments_above_are_docstrings(
    header: dict[str, Symbol], router: dict[str, Symbol]
) -> None:
    assert header["Method#"].docstring == "What a route answers to."
    assert header["Router#"].docstring == (
        "Routes requests to the handler registered for their method and path."
    )
    assert router["Router#add()."].docstring == (
        "Register a handler; routes match in the order they were added."
    )
    assert header["Registry#"].docstring is None


def test_base_classes_are_bases(header: dict[str, Symbol]) -> None:
    assert header["Router#"].attrs == {"bases": ["Component", "NonCopyable"]}
    based = _symbols("a.cpp", "struct Repo : public virtual Base<int>, protected ns::Store {};\n")
    assert based["Repo#"].attrs == {"subkind": "struct", "bases": ["Base<int>", "ns::Store"]}


# ---------------------------------------------------------------------------- end to end


def test_a_cpp_file_is_parsed_keyed_and_chunked_by_definition() -> None:
    path = "src/router.cpp"
    result = parse_file(path, SOURCE.encode())  # the language comes from the extension
    assert (result.status, result.language) == (ParseStatus.PARSED, "cpp")
    assert all(result.captures[k] for k in (QueryKind.DEFS, QueryKind.IMPORTS, QueryKind.CALLS))
    chunks = chunk_file(path, "cpp", SOURCE)
    keyed = [(c.symbol_key or "").removeprefix(f"cpp {path}/") for c in chunks]
    # The small definitions merge; the long method is a chunk of its own.
    assert keyed == ["", "Router#dispatch().", ""]
    dispatch = chunks[1]
    assert (dispatch.kind.value, dispatch.start_line, dispatch.end_line) == ("method", 25, 36)
    assert "lang: cpp" in dispatch.header
    assert dispatch.content.startswith("Response Router::dispatch(const Request& request) const {")


def test_a_class_with_methods_is_its_header_then_its_members() -> None:
    path = "include/shop/router.hpp"
    chunks = chunk_file(path, "cpp", HEADER)
    keyed = [(c.symbol_key or "").removeprefix(f"cpp {path}/") for c in chunks]
    # Route, a struct, is a chunk of its own (C++'s hold code); Router's header (its doc
    # comment down to its first member), then its members merged, small as they are.
    assert keyed == ["", "Route#", "Router#", "", "Registry#"]
    router = chunks[2]
    assert (router.start_line, router.end_line) == (22, 26)
    assert router.content.startswith("/**\n * Routes requests")


# ---------------------------------------------------------------------------- lanes


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/router_test.cc", "test"),
        ("src/router_unittest.cpp", "test"),
        ("test/router.cpp", "test"),
        ("src/router.cpp", "service"),
        ("include/shop/router.hpp", "service"),
    ],
)
def test_googletest_files_are_tests(path: str, expected: str) -> None:
    assert assign_role(path, "cpp", "int x;\n", ui_package=False).value == expected
