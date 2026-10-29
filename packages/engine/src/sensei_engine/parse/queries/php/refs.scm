; References: names, variables and names used as types. A node two patterns capture takes the
; first one's kind.

; A method's name in a call (find in $this->orders->find($id)): not a free name.
(member_call_expression
  name: (name) @reference.method)

(nullsafe_member_call_expression
  name: (name) @reference.method)

(scoped_call_expression
  name: (name) @reference.method)

; $obj-><name> (a property read)
(member_access_expression
  name: (name) @reference.field)

(nullsafe_member_access_expression
  name: (name) @reference.field)

(named_type
  [(name) (qualified_name)] @reference.type)

(variable_name
  (name) @reference.variable)

(name) @reference.identifier
