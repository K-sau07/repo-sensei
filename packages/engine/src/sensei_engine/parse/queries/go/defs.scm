; Go definitions. The main capture (@definition.<kind>) is the definition node, @name its name.
; A method is keyed under its receiver's type (@owner: Server in func (s *Server) Handle), which
; may be declared in another file of the package. Patterns after tree-sitter-go's tags query
; (MIT).

(package_clause
  (package_identifier) @name) @definition.package

; A type_spec per type, one declaration holding one or several (type ( A struct{}; B int )).
(type_spec
  name: (type_identifier) @name
  type: (struct_type)) @definition.struct

(type_spec
  name: (type_identifier) @name
  type: (interface_type)) @definition.interface

(type_spec
  name: (type_identifier) @name) @definition.type

(type_alias
  name: (type_identifier) @name) @definition.type

(function_declaration
  name: (identifier) @name) @definition.function

(method_declaration
  receiver: (parameter_list
    (parameter_declaration
      type: [
        (type_identifier) @owner
        (pointer_type (type_identifier) @owner)
        (generic_type type: (type_identifier) @owner)
        (pointer_type (generic_type type: (type_identifier) @owner))
      ]))
  name: (field_identifier) @name) @definition.method

; A struct's named fields (an embedded type is a base: see the symbols) and an interface's
; methods.
(struct_type
  (field_declaration_list
    (field_declaration
      name: (field_identifier) @name) @definition.field))

(interface_type
  (method_elem
    name: (field_identifier) @name) @definition.method)

; Package-level constants and variables, one per name (var a, b int: two).
(source_file
  (const_declaration
    (const_spec
      name: (identifier) @name) @definition.variable))

(source_file
  (var_declaration
    (var_spec
      name: (identifier) @name) @definition.variable))

(source_file
  (var_declaration
    (var_spec_list
      (var_spec
        name: (identifier) @name) @definition.variable)))
