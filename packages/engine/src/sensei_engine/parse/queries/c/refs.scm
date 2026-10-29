; References: identifiers and names used as types (C++ inherits them). A node two patterns
; capture takes the first one's kind.

; A function pointer's name in a call through a struct (open in dev->ops->open(dev)): not a free
; name.
(call_expression
  function: (field_expression
    field: (field_identifier) @reference.method))

; s.<name>, p-><name> (a field read)
(field_expression
  field: (field_identifier) @reference.field)

(type_identifier) @reference.type

(identifier) @reference.identifier
