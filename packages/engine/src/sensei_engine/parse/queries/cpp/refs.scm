; C++ references, before the C ones it inherits. A node two patterns capture takes the first
; one's kind.

; A method's name in a call (find in repo.find(id), get in obj.get<int>()): not a free name.
(call_expression
  function: (field_expression
    field: [
      (field_identifier) @reference.method
      (template_method name: (field_identifier) @reference.method)
    ]))

; A scope: a namespace or a class (std in std::move(x), Box in Box::make()).
(namespace_identifier) @reference.scope
