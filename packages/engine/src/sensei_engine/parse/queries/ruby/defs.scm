; Ruby definitions. The main capture (@definition.<kind>) is the definition node, @name its name.
; A node two patterns capture takes the first one's kind, so specific patterns come first.
; Modules and classes nest (module Api; module V1; class OrdersController): each is keyed under
; what holds it, and a compact name's scope (class Api::V1::OrdersController) is its @owner, so
; both spellings key the class alike. Patterns after tree-sitter-ruby's tags query (MIT).

(module
  name: (scope_resolution
    scope: (_) @owner
    name: (constant) @name)) @definition.mod

(module
  name: [
    (constant) @name
    (scope_resolution !scope name: (constant) @name)
  ]) @definition.mod

(class
  name: (scope_resolution
    scope: (_) @owner
    name: (constant) @name)) @definition.class

(class
  name: [
    (constant) @name
    (scope_resolution !scope name: (constant) @name)
  ]) @definition.class

; def self.find_by_token: a class method, as the methods in class << self are.
(singleton_method
  object: (self)
  name: (_) @name) @definition.classmethod

(singleton_class
  value: (self)
  body: (body_statement
    (method
      name: (_) @name) @definition.classmethod))

; Instance methods: defs in a class or module body; any other def (top-level, in a block) is a
; function.
(class
  body: (body_statement
    (method
      name: (_) @name) @definition.method))

(module
  body: (body_statement
    (method
      name: (_) @name) @definition.method))

(method
  name: (_) @name) @definition.function

; Constants: a class's or module's (MAX_PAGE = 50), and a file's.
(class
  body: (body_statement
    (assignment
      left: (constant) @name) @definition.field))

(module
  body: (body_statement
    (assignment
      left: (constant) @name) @definition.field))

(program
  (assignment
    left: (constant) @name) @definition.variable)
