; References: identifiers and names used as types. A node two patterns capture takes the first
; one's kind.

; obj.<name> (a field read; a method's name is in calls.scm)
(field_access
  field: (identifier) @reference.field)

; A method's name in a call (find in orderService.find(id)): not a free name.
(method_invocation
  name: (identifier) @reference.method)

(type_identifier) @reference.type

(identifier) @reference.identifier
