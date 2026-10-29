; References: identifiers and names used as types. A node two patterns capture takes the first
; one's kind.

; A method's name in a call (Find in s.store.Find(ctx, id)): not a free name.
(call_expression
  function: (selector_expression
    field: (field_identifier) @reference.method))

; x.<name> (a field read)
(selector_expression
  field: (field_identifier) @reference.field)

(package_identifier) @reference.package

(type_identifier) @reference.type

(identifier) @reference.identifier
