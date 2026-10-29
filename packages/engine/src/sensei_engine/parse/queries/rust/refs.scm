; References: identifiers and names used as types. A node two patterns capture takes the first
; one's kind.

; A method's name in a call (find in self.repo.find(id)): not a free name.
(call_expression
  function: (field_expression
    field: (field_identifier) @reference.method))

; obj.<name> (a field read)
(field_expression
  field: (field_identifier) @reference.field)

(type_identifier) @reference.type

(identifier) @reference.identifier
