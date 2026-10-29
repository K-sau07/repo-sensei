; Java definitions. The main capture (@definition.<kind>) is the definition node, @name its name.
; Types nest at any depth (inner, static nested and local classes, an enum in an interface):
; each is captured where it stands and keyed under what holds it.

(class_declaration
  name: (identifier) @name) @definition.class

(interface_declaration
  name: (identifier) @name) @definition.interface

(enum_declaration
  name: (identifier) @name) @definition.enum

(record_declaration
  name: (identifier) @name) @definition.record

; @interface Audited {...}
(annotation_type_declaration
  name: (identifier) @name) @definition.annotation

; Methods anywhere: in classes, interfaces (abstract or default), enums, records, an enum
; constant's body and anonymous classes. An annotation type's elements (String value() default
; "") are its methods too.
(method_declaration
  name: (identifier) @name) @definition.method

(annotation_type_element_declaration
  name: (identifier) @name) @definition.method

; Constructors, and a record's compact constructor (record Point(int x, int y) { Point {...} }).
(constructor_declaration
  name: (identifier) @name) @definition.constructor

(compact_constructor_declaration
  name: (identifier) @name) @definition.constructor

; Fields, one per declarator (private int a, b: two fields), the constants of interfaces and
; annotation types, enum constants and a record's components.
(field_declaration
  declarator: (variable_declarator
    name: (identifier) @name) @definition.field)

(constant_declaration
  declarator: (variable_declarator
    name: (identifier) @name) @definition.field)

(enum_constant
  name: (identifier) @name) @definition.field

(record_declaration
  parameters: (formal_parameters
    (formal_parameter
      name: (identifier) @name) @definition.field))
