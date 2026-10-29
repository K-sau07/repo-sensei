; Calls. @name: the called name; @receiver: what it is called on (self.repo in
; self.repo.find(id); a type or a module for a path call: Order in Order::find(id), sqlx in
; sqlx::query(sql)); @arguments.

; Order::new(...), the constructor by convention (@name the type).
((call_expression
   function: (scoped_identifier
     path: (_) @name
     name: (identifier) @_new)
   arguments: (arguments) @arguments) @call.new
 (#eq? @_new "new"))

(call_expression
  function: (field_expression
    value: (_) @receiver
    field: (field_identifier) @name)
  arguments: (arguments) @arguments) @call.method

((call_expression
   function: (scoped_identifier
     path: (_) @receiver
     name: (identifier) @name)
   arguments: (arguments) @arguments) @call.method
 (#not-eq? @name "new"))

; iter.collect::<Vec<_>>(), parse::<i64>(): the name without its type arguments.
(call_expression
  function: (generic_function
    function: (field_expression
      value: (_) @receiver
      field: (field_identifier) @name))
  arguments: (arguments) @arguments) @call.method

(call_expression
  function: (generic_function
    function: (scoped_identifier
      path: (_) @receiver
      name: (identifier) @name))
  arguments: (arguments) @arguments) @call.method

(call_expression
  function: (generic_function
    function: (identifier) @name)
  arguments: (arguments) @arguments) @call

(call_expression
  function: (identifier) @name
  arguments: (arguments) @arguments) @call

; Struct literals: Order { id, total }, Self { .. } (@name the type; @arguments the fields).
(struct_expression
  name: [(type_identifier) (scoped_type_identifier)] @name
  body: (field_initializer_list) @arguments) @call.new

; Macros: println!(...), sqlx::query!(...), vec![...] (@name the macro, without its !).
(macro_invocation
  macro: [(identifier) (scoped_identifier)] @name
  (token_tree) @arguments) @call.macro
