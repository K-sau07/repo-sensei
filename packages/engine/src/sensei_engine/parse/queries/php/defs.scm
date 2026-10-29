; PHP definitions. The main capture (@definition.<kind>) is the definition node, @name its name
; ($ left out of a property's). A node two patterns capture takes the first one's kind, so
; specific patterns come first. A namespace is a definition, but not what its classes are keyed
; under (see the symbols). Patterns after tree-sitter-php's tags query (MIT).

(namespace_definition
  name: (namespace_name) @name) @definition.module

(class_declaration
  name: (name) @name) @definition.class

(interface_declaration
  name: (name) @name) @definition.interface

(trait_declaration
  name: (name) @name) @definition.trait

(enum_declaration
  name: (name) @name) @definition.enum

((method_declaration
   name: (name) @name) @definition.constructor
 (#eq? @name "__construct"))

(method_declaration
  name: (name) @name) @definition.method

(function_definition
  name: (name) @name) @definition.function

; Properties, one per element (protected $a, $b: two), constructor-promoted ones (public function
; __construct(private OrderService $orders): a property of the class), class constants and enum
; cases.
(property_declaration
  (property_element
    name: (variable_name
      (name) @name)) @definition.field)

(property_promotion_parameter
  name: (variable_name
    (name) @name)) @definition.field

(declaration_list
  (const_declaration
    (const_element
      (name) @name) @definition.field))

(enum_case
  name: (name) @name) @definition.field

; A file's constants (const VERSION = '1'; outside a class).
(program
  (const_declaration
    (const_element
      (name) @name) @definition.variable))
