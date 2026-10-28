; Calls. @name: the called name; @receiver: what it is called on (obj in obj.f()); @arguments
; (a template string for tagged templates).

(call_expression
  function: (identifier) @name
  arguments: (_) @arguments) @call

(call_expression
  function: (member_expression
    object: (_) @receiver
    property: (_) @name)
  arguments: (_) @arguments) @call.method

; ``await f<T>(x)`` / ``await api.get<T>(url)``: with type arguments, tree-sitter's TypeScript puts
; the ``await`` inside the call, around its function.
(call_expression
  function: (await_expression (identifier) @name)
  arguments: (_) @arguments) @call

(call_expression
  function: (await_expression
    (member_expression
      object: (_) @receiver
      property: (_) @name))
  arguments: (_) @arguments) @call.method

(new_expression
  constructor: (_) @name
  arguments: (_)? @arguments) @call.new
