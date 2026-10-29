; References: identifiers and names used as types. A node two patterns capture takes the first
; one's kind.

; A method's name in a call (FindAsync in _db.Orders.FindAsync(id)): not a free name.
(invocation_expression
  function: (member_access_expression
    name: (identifier) @reference.method))

; obj.<name> (a property or field read)
(member_access_expression
  name: (identifier) @reference.field)

(identifier) @reference.identifier
