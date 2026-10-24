; Calls. @name: the called name; @receiver: what it is called on (obj in obj.f()); @arguments.

(call
  function: (identifier) @name
  arguments: (_) @arguments) @call

(call
  function: (attribute
    object: (_) @receiver
    attribute: (identifier) @name)
  arguments: (_) @arguments) @call.method
