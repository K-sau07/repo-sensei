; Kotlin definitions. The main capture (@definition.<kind>) is the definition node, @name its
; name. A node two patterns capture takes the first one's kind, so specific patterns come first.
; Types nest at any depth (inner and nested classes, companion objects): each is captured where
; it stands and keyed under what holds it.

(class_declaration
  "interface"
  name: (identifier) @name) @definition.interface

((class_declaration
   (modifiers (class_modifier) @_modifier)
   name: (identifier) @name) @definition.enum
 (#eq? @_modifier "enum"))

((class_declaration
   (modifiers (class_modifier) @_modifier)
   name: (identifier) @name) @definition.data_class
 (#eq? @_modifier "data"))

((class_declaration
   (modifiers (class_modifier) @_modifier)
   name: (identifier) @name) @definition.annotation
 (#eq? @_modifier "annotation"))

(class_declaration
  name: (identifier) @name) @definition.class

(object_declaration
  name: (identifier) @name) @definition.object

; companion object (named Companion unless it is given a name)
(companion_object
  name: (identifier) @name) @definition.companion

(companion_object
  !name) @definition.companion

(type_alias
  type: (identifier) @name) @definition.type

; Functions in a class, object or enum body are its methods; any other function (top-level,
; local, an extension) is a function.
(class_body
  (function_declaration
    name: (identifier) @name) @definition.method)

(enum_class_body
  (function_declaration
    name: (identifier) @name) @definition.method)

(function_declaration
  name: (identifier) @name) @definition.function

; constructor(...): named after its class, as Java's are.
(class_declaration
  name: (identifier) @name
  (class_body
    (secondary_constructor) @definition.constructor))

(class_declaration
  name: (identifier) @name
  (enum_class_body
    (secondary_constructor) @definition.constructor))

; Properties: a class's and an object's, the val and var parameters of a primary constructor,
; enum entries; a file's top-level ones are variables. A local val is neither.
(class_body
  (property_declaration
    (variable_declaration
      (identifier) @name)) @definition.field)

(enum_class_body
  (property_declaration
    (variable_declaration
      (identifier) @name)) @definition.field)

(class_parameter
  ["val" "var"]
  .
  (identifier) @name) @definition.field

(enum_entry
  (identifier) @name) @definition.field

(source_file
  (property_declaration
    (variable_declaration
      (identifier) @name)) @definition.variable)
