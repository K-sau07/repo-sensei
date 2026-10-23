; TypeScript definitions, before the shared ones (ecma/defs.scm).

(interface_declaration
  name: (_) @name) @definition.interface

(type_alias_declaration
  name: (_) @name) @definition.type

(enum_declaration
  name: (_) @name) @definition.enum

(abstract_class_declaration
  name: (_) @name) @definition.class

; namespace N {...}; declare module "m" {...}
(internal_module
  name: (_) @name) @definition.module

(module
  name: (_) @name) @definition.module

; Overload and ambient signatures (declare function f(): T;), methods without a body.
(function_signature
  name: (_) @name) @definition.function

(method_signature
  name: (_) @name) @definition.method

(abstract_method_signature
  name: (_) @name) @definition.method

(public_field_definition
  name: (_) @name) @definition.field

(property_signature
  name: (_) @name) @definition.field

; declare const x: T;
(program
  (ambient_declaration
    [(lexical_declaration (variable_declarator name: (identifier) @name) @definition.variable)
     (variable_declaration (variable_declarator name: (identifier) @name) @definition.variable)]))
