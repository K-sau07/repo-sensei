; C definitions (C++ inherits them). The main capture (@definition.<kind>) is the definition
; node, @name its name. A node two patterns capture takes the first one's kind, so specific
; patterns come first. A declarator's name isn't captured: declarators nest without limit
; (int *(*handlers[4])(void)), so the symbols find it, and whether it declares a function (a
; prototype is a function, a pointer to one a variable). A declaration's declarators are
; captured one by one wherever it stands; those in a function body are its locals, which the
; symbols drop. Patterns after tree-sitter-c's tags query (MIT).

(function_definition) @definition.function

; Structs, unions and enums with a body (struct node *next; only names one). A typedef of an
; anonymous one names it: typedef struct { ... } point_t; is the struct point_t.
(struct_specifier
  name: (type_identifier) @name
  body: (_)) @definition.struct

(type_definition
  type: (struct_specifier
    !name
    body: (_)) @definition.struct
  .
  declarator: (type_identifier) @name)

(union_specifier
  name: (type_identifier) @name
  body: (_)) @definition.union

(type_definition
  type: (union_specifier
    !name
    body: (_)) @definition.union
  .
  declarator: (type_identifier) @name)

(enum_specifier
  name: (type_identifier) @name
  body: (_)) @definition.enum

(type_definition
  type: (enum_specifier
    !name
    body: (_)) @definition.enum
  .
  declarator: (type_identifier) @name)

(enumerator
  name: (identifier) @name) @definition.field

; A struct's or a union's fields, one per declarator (int x, y: two).
(field_declaration_list
  (field_declaration
    declarator: (_) @definition.field))

; Typedefs, one per declarator (typedef int (*cmp_fn)(const void *, const void *); too).
(type_definition
  declarator: (_) @definition.type)

; Prototypes and globals, one per declarator (int a = 1, b;).
(declaration
  declarator: (_) @definition.variable)

; Macros: function-like ones are callables, object-like ones with a value constants (an include
; guard's #define FOO_H, with none, isn't a symbol).
(preproc_function_def
  name: (identifier) @name) @definition.macro

(preproc_def
  name: (identifier) @name
  value: (_)) @definition.macro_value
