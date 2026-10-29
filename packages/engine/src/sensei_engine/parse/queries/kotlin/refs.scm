; References: identifiers and names used as types. A node two patterns capture takes the first
; one's kind.

; A method's name in a call (find in orderService.find(id)): not a free name.
(call_expression
  (navigation_expression
    (identifier) @reference.method .))

; obj.<name> (a property read)
(navigation_expression
  (identifier) @reference.field .)

(user_type
  (identifier) @reference.type)

(identifier) @reference.identifier
