; Calls. @name: the called name, without type arguments; @receiver: what it is called on
; (_orders in _orders.FindAsync(id); this, base, a class for a static call, a chained call);
; @arguments.

(invocation_expression
  function: (member_access_expression
    expression: _ @receiver
    name: [
      (identifier) @name
      (generic_name (identifier) @name)
    ])
  arguments: (argument_list) @arguments) @call.method

; order?.Total()
(invocation_expression
  function: (conditional_access_expression
    condition: (_) @receiver
    (member_binding_expression
      name: [
        (identifier) @name
        (generic_name (identifier) @name)
      ]))
  arguments: (argument_list) @arguments) @call.method

(invocation_expression
  function: [
    (identifier) @name
    (generic_name (identifier) @name)
  ]
  arguments: (argument_list) @arguments) @call

; new OrderDto(...), new OrderDto { Id = id }, new List<Order>(): @name the type without its
; type arguments; @arguments the argument list, when there is one (an object initializer alone
; has none).
(object_creation_expression
  type: (generic_name
    (identifier) @name)
  arguments: (argument_list)? @arguments) @call.new

(object_creation_expression
  type: [(identifier) (qualified_name)] @name
  arguments: (argument_list)? @arguments) @call.new

; : base(...) and : this(...) after a constructor's parameters: @name is the keyword.
(constructor_initializer
  ["base" "this"] @name
  (argument_list) @arguments) @call.constructor
