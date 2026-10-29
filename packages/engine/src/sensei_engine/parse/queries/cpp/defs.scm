; C++ definitions, before the C ones it inherits (a node two patterns capture takes the first
; one's kind). A namespace is a definition, but not what its contents are keyed under (see the
; symbols), as a C# one isn't; an anonymous one (namespace { ... }) isn't one at all. A
; template's name is without its arguments (Box for template <> class Box<int>). A method
; defined outside its class (void Box::run() {}) is a function definition the symbols key under
; its class, as Go's methods are under their receiver's type. Patterns after tree-sitter-cpp's
; tags query (MIT).

(namespace_definition
  name: (_) @name) @definition.module

(class_specifier
  name: [
    (type_identifier) @name
    (template_type name: (type_identifier) @name)
  ]
  body: (_)) @definition.class

(struct_specifier
  name: (template_type name: (type_identifier) @name)
  body: (_)) @definition.struct

; Methods defined in a class body, templates among them.
(field_declaration_list
  [(function_definition) @definition.method
   (template_declaration
     (function_definition) @definition.method)])

; Methods declared in a class body by a declaration: constructors, destructors, conversion
; operators, templates (the others, and data members, are field declarations: the symbols tell
; a method's from a field's by its declarator).
(field_declaration_list
  [(declaration
     declarator: (_) @definition.method)
   (template_declaration
     (declaration
       declarator: (_) @definition.method))])

; A lambda held in a variable is a function (auto add = [](int a, int b) { ... };).
(declaration
  declarator: (init_declarator
    value: (lambda_expression)) @definition.function)

; using Orders = std::vector<Order>; (and its template form)
(alias_declaration
  name: (type_identifier) @name) @definition.type
