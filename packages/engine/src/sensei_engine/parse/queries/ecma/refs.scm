; References: identifiers and property accesses. A node two patterns capture takes the first
; one's kind.

; obj.<name>
(member_expression
  property: (_) @reference.property)

; { a } in an object literal refers to the variable a.
(shorthand_property_identifier) @reference.identifier

(identifier) @reference.identifier
