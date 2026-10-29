; Calls. @name: the called name; @receiver: what it is called on (orderService in
; orderService.find(id); this, super, a class for a companion's function, a chained call);
; @arguments: the argument list, or the trailing lambda of a call without one (routing { ... }).
; Kotlin has no new: Order(id) is a bare call, a constructor's or a function's.

(call_expression
  (navigation_expression
    .
    (_) @receiver
    (identifier) @name .)
  (value_arguments) @arguments) @call.method

(call_expression
  (navigation_expression
    .
    (_) @receiver
    (identifier) @name .)
  .
  (annotated_lambda) @arguments) @call.method

(call_expression
  .
  (identifier) @name
  (value_arguments) @arguments) @call

(call_expression
  .
  (identifier) @name
  .
  (annotated_lambda) @arguments) @call

; this(...) and super(...) in a secondary constructor: @name is the keyword.
(constructor_delegation_call
  ["this" "super"] @name
  (value_arguments) @arguments) @call.constructor

; The superclass's constructor a class calls in its header (: BaseController()).
(delegation_specifier
  (constructor_invocation
    (user_type) @name
    (value_arguments) @arguments) @call.constructor)

; Callable references, called later by whatever they are passed to: Order::from, ::toDto.
(navigation_expression
  .
  (_) @receiver
  "::"
  (identifier) @name .) @call.reference

(callable_reference
  (identifier) @name .) @call.reference
