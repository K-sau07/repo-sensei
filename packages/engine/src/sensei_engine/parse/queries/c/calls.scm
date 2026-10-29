; Calls (C++ inherits them). @name: the called name; @receiver: what it is called through (a
; struct or a pointer to one holding a function pointer: dev->ops in dev->ops->open(dev), s in
; s.cb(x)); @arguments. A macro called like a function (MAX(a, b)) is a call too.

(call_expression
  function: (field_expression
    argument: (_) @receiver
    field: (field_identifier) @name)
  arguments: (argument_list) @arguments) @call.method

(call_expression
  function: (identifier) @name
  arguments: (argument_list) @arguments) @call

; (*handler)(req): the pointer called through.
(call_expression
  function: (parenthesized_expression
    (pointer_expression
      argument: (identifier) @name))
  arguments: (argument_list) @arguments) @call
