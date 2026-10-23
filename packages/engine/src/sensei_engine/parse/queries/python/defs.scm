; Definitions. The main capture (@definition.<kind>) is the definition node, @name its name.
; A node two patterns capture takes the first one's kind, so specific patterns come first.
; A decorated definition is captured without its decorators (see decorators.scm).

; Methods: functions directly in a class body.
(class_definition
  body: (block
    (function_definition
      name: (identifier) @name) @definition.method))

(class_definition
  body: (block
    (decorated_definition
      definition: (function_definition
        name: (identifier) @name) @definition.method)))

(class_definition
  name: (identifier) @name) @definition.class

(function_definition
  name: (identifier) @name) @definition.function

; Class attributes (model fields, settings, enum members).
(class_definition
  body: (block
    (expression_statement
      (assignment
        left: (identifier) @name) @definition.field)))

; Module-level names: a lambda is a function, anything else a variable.
(module
  (expression_statement
    (assignment
      left: (identifier) @name
      right: (lambda)) @definition.function))

(module
  (expression_statement
    (assignment
      left: (identifier) @name) @definition.variable))

(module
  (type_alias_statement
    left: (type
      (identifier) @name)) @definition.type)

; Exports: the names listed in __all__.
(module
  (expression_statement
    (assignment
      left: (identifier) @_all
      right: [(list (string) @name) (tuple (string) @name)]) @export.all)
  (#eq? @_all "__all__"))
