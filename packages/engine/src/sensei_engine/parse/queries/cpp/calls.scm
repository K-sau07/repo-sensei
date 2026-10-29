; C++ calls, before the C ones it inherits. @name: the called name, without template arguments;
; @receiver: what it is called on (repo in repo.find(id), this in this->run(); a class or a
; namespace for a qualified call: Box in Box::make(), std in std::sort(v)). A qualified name's
; scope is captured a part at a time, outermost first (std, chrono, steady_clock in
; std::chrono::steady_clock::now()): consumers join them with ::. @arguments.

; std::make_unique<Order>(...) and make_shared construct their type argument (@name: Order).
((call_expression
   function: [
     (template_function
       name: (identifier) @_make
       arguments: (template_argument_list
         .
         (type_descriptor
           type: [
             (type_identifier) @name
             (template_type name: (type_identifier) @name)
             (qualified_identifier) @name
           ])))
     (qualified_identifier
       name: (template_function
         name: (identifier) @_make
         arguments: (template_argument_list
           .
           (type_descriptor
             type: [
               (type_identifier) @name
               (template_type name: (type_identifier) @name)
               (qualified_identifier) @name
             ]))))
   ]
   arguments: (argument_list) @arguments) @call.new
 (#any-of? @_make "make_unique" "make_shared"))

; obj.m(), ptr->m(), obj.get<int>(), obj.template get<int>(), obj.Base::m(), p->~T().
(call_expression
  function: (field_expression
    argument: (_) @receiver
    field: [
      (field_identifier) @name
      (template_method name: (field_identifier) @name)
      (dependent_name (template_method name: (field_identifier) @name))
      (qualified_identifier name: (field_identifier) @name)
      (destructor_name) @name
    ])
  arguments: (argument_list) @arguments) @call.method

; Ns::f(), Type::static_m(), a::b::f<T>() (up to four scopes).
((call_expression
   function: (qualified_identifier
     scope: (_) @receiver
     name: [
       (identifier) @name
       (template_function name: (identifier) @name)
     ])
   arguments: (argument_list) @arguments) @call.method
 (#not-match? @name "^make_(unique|shared)$"))

(call_expression
  function: (qualified_identifier
    scope: (_) @receiver
    name: (qualified_identifier
      scope: (_) @receiver
      name: [
        (identifier) @name
        (template_function name: (identifier) @name)
      ]))
  arguments: (argument_list) @arguments) @call.method

(call_expression
  function: (qualified_identifier
    scope: (_) @receiver
    name: (qualified_identifier
      scope: (_) @receiver
      name: (qualified_identifier
        scope: (_) @receiver
        name: [
          (identifier) @name
          (template_function name: (identifier) @name)
        ])))
  arguments: (argument_list) @arguments) @call.method

(call_expression
  function: (qualified_identifier
    scope: (_) @receiver
    name: (qualified_identifier
      scope: (_) @receiver
      name: (qualified_identifier
        scope: (_) @receiver
        name: (qualified_identifier
          scope: (_) @receiver
          name: [
            (identifier) @name
            (template_function name: (identifier) @name)
          ]))))
  arguments: (argument_list) @arguments) @call.method

; ::f() (the global one), f<T>(x); not a cast (static_cast<T*>(p) reads as a call).
(call_expression
  function: (qualified_identifier
    !scope
    name: [
      (identifier) @name
      (template_function name: (identifier) @name)
    ])
  arguments: (argument_list) @arguments) @call

((call_expression
   function: (template_function
     name: (identifier) @name)
   arguments: (argument_list) @arguments) @call
 (#not-match? @name "^(make_(unique|shared)|(static|dynamic|const|reinterpret)_cast)$"))

; new Order(id), new ns::Box<int>{}, new Order[n] (@name the type without its template
; arguments; @receiver its scope; @arguments the argument or initializer list, when there is
; one).
(new_expression
  type: [
    (type_identifier) @name
    (template_type name: (type_identifier) @name)
  ]
  arguments: (_)? @arguments) @call.new

(new_expression
  type: (qualified_identifier
    scope: (_) @receiver
    name: [
      (type_identifier) @name
      (template_type name: (type_identifier) @name)
    ])
  arguments: (_)? @arguments) @call.new

; Route{method, path}, Box<int>{}: a temporary braced into being.
(compound_literal_expression
  type: [
    (type_identifier) @name
    (template_type name: (type_identifier) @name)
  ]
  value: (initializer_list) @arguments) @call.new

(compound_literal_expression
  type: (qualified_identifier
    scope: (_) @receiver
    name: [
      (type_identifier) @name
      (template_type name: (type_identifier) @name)
    ])
  value: (initializer_list) @arguments) @call.new

; A variable constructed where it is declared: Order order(id); Box<int> box{1};
; std::string s("x"); (the main capture is its declarator).
(declaration
  type: [
    (type_identifier) @name
    (template_type name: (type_identifier) @name)
  ]
  declarator: (init_declarator
    value: [(argument_list) (initializer_list)] @arguments) @call.new)

(declaration
  type: (qualified_identifier
    scope: (_) @receiver
    name: [
      (type_identifier) @name
      (template_type name: (type_identifier) @name)
    ])
  declarator: (init_declarator
    value: [(argument_list) (initializer_list)] @arguments) @call.new)
