; Calls. @name: the called name; @receiver: what it is called on (orderService in
; orderService.find(id); this, super, a class for a static call, a chained call); @arguments.

(method_invocation
  object: (_) @receiver
  name: (identifier) @name
  arguments: (argument_list) @arguments) @call.method

(method_invocation
  !object
  name: (identifier) @name
  arguments: (argument_list) @arguments) @call

; new OrderDto(...), new ArrayList<>(): @name is the class without its type arguments (an
; anonymous class's body is part of the call).
(object_creation_expression
  type: (generic_type
    .
    [(type_identifier) (scoped_type_identifier)] @name)
  arguments: (argument_list) @arguments) @call.new

(object_creation_expression
  type: [(type_identifier) (scoped_type_identifier)] @name
  arguments: (argument_list) @arguments) @call.new

; this(...) and super(...) in a constructor: @name is the keyword.
(explicit_constructor_invocation
  constructor: [(this) (super)] @name
  arguments: (argument_list) @arguments) @call.constructor

; Method references, called later by whatever they are passed to: OrderMapper::toDto,
; this::handle, and constructor references (Order::new, @name the class).
(method_reference
  .
  (_) @receiver
  (identifier) @name .) @call.reference

(method_reference
  .
  (_) @name
  "new") @call.reference.new
