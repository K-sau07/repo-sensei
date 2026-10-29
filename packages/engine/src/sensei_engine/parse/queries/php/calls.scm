; Calls. @name: the called name; @receiver: what it is called on ($this->orders in
; $this->orders->find($id); a class, self, static or parent for a static call: Order in
; Order::findOrFail($id)); @arguments.

(member_call_expression
  object: (_) @receiver
  name: (name) @name
  arguments: (arguments) @arguments) @call.method

(nullsafe_member_call_expression
  object: (_) @receiver
  name: (name) @name
  arguments: (arguments) @arguments) @call.method

(scoped_call_expression
  scope: (_) @receiver
  name: (name) @name
  arguments: (arguments) @arguments) @call.method

(function_call_expression
  function: [(name) (qualified_name)] @name
  arguments: (arguments) @arguments) @call

; new OrderDto(...), new \App\Models\Order, new static(): @name the class; @arguments the
; argument list, when there is one.
(object_creation_expression
  [(name) (qualified_name)] @name
  (arguments)? @arguments) @call.new
