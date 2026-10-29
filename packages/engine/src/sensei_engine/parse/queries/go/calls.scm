; Calls. @name: the called name; @receiver: what it is called on (s.store in s.store.Find(ctx,
; id); a package for a function of another package: json in json.Marshal(v)); @arguments.

(call_expression
  function: (selector_expression
    operand: (_) @receiver
    field: (field_identifier) @name)
  arguments: (argument_list) @arguments) @call.method

(call_expression
  function: (identifier) @name
  arguments: (argument_list) @arguments) @call

; Composite literals of a named type, Go's construction: Order{ID: 1}, &models.Order{},
; Page[Order]{} (@name the type without its type arguments; @arguments the literal's body).
(composite_literal
  type: (generic_type
    type: [(type_identifier) (qualified_type)] @name)
  body: (literal_value) @arguments) @call.new

(composite_literal
  type: [(type_identifier) (qualified_type)] @name
  body: (literal_value) @arguments) @call.new
