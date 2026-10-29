; C# definitions. The main capture (@definition.<kind>) is the definition node, @name its name.
; Types nest at any depth (nested classes, a type in a namespace block): each is captured where
; it stands. A namespace is a definition, but not what its types are keyed under (see the
; symbols): file-scoped and block namespaces key a class alike.

(namespace_declaration
  name: (_) @name) @definition.module

(file_scoped_namespace_declaration
  name: (_) @name) @definition.module

(class_declaration
  name: (identifier) @name) @definition.class

(interface_declaration
  name: (identifier) @name) @definition.interface

(struct_declaration
  name: (identifier) @name) @definition.struct

(record_declaration
  name: (identifier) @name) @definition.record

(enum_declaration
  name: (identifier) @name) @definition.enum

(delegate_declaration
  name: (identifier) @name) @definition.type

; Methods anywhere (classes, interfaces, structs, records), and local functions.
(method_declaration
  name: (identifier) @name) @definition.method

(local_function_statement
  name: (identifier) @name) @definition.function

(constructor_declaration
  name: (identifier) @name) @definition.constructor

; Properties ({ get; set; } and => expression ones), apart from fields.
(property_declaration
  name: (identifier) @name) @definition.property

; Fields and events, one per declarator (private int a, b: two fields), enum members, and a
; record's positional parameters (record OrderDto(int Id): a property Id).
(field_declaration
  (variable_declaration
    (variable_declarator
      name: (identifier) @name) @definition.field))

(event_field_declaration
  (variable_declaration
    (variable_declarator
      name: (identifier) @name) @definition.field))

(enum_member_declaration
  name: (identifier) @name) @definition.field

(record_declaration
  (parameter_list
    (parameter
      name: (identifier) @name) @definition.property))
