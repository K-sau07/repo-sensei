; References: identifiers, constants and instance variables. A node two patterns capture takes
; the first one's kind.

; A method's name in a call (find in Order.find(id)): not a free name.
(call
  method: (identifier) @reference.method)

(constant) @reference.constant

(instance_variable) @reference.field

(identifier) @reference.identifier
