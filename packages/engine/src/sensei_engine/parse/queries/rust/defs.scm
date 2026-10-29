; Rust definitions. The main capture (@definition.<kind>) is the definition node, @name its name.
; A node two patterns capture takes the first one's kind, so specific patterns come first. An
; impl block isn't a definition: its functions and constants are keyed under its type (@owner:
; Order in impl Order and in impl Display for Order). Patterns after tree-sitter-rust's tags
; query (MIT).

(mod_item
  name: (identifier) @name) @definition.mod

(struct_item
  name: (type_identifier) @name) @definition.struct

(enum_item
  name: (type_identifier) @name) @definition.enum

(union_item
  name: (type_identifier) @name) @definition.union

(trait_item
  name: (type_identifier) @name) @definition.trait

(type_item
  name: (type_identifier) @name) @definition.type

(macro_definition
  name: (identifier) @name) @definition.macro

; An impl's functions are methods of its type (associated functions, new among them, too).
(impl_item
  type: [
    (type_identifier) @owner
    (generic_type type: (type_identifier) @owner)
    (scoped_type_identifier name: (type_identifier) @owner)
    (generic_type type: (scoped_type_identifier name: (type_identifier) @owner))
  ]
  body: (declaration_list
    (function_item
      name: (identifier) @name) @definition.method))

(impl_item
  type: [
    (type_identifier) @owner
    (generic_type type: (type_identifier) @owner)
    (scoped_type_identifier name: (type_identifier) @owner)
    (generic_type type: (scoped_type_identifier name: (type_identifier) @owner))
  ]
  body: (declaration_list
    (const_item
      name: (identifier) @name) @definition.field))

; A trait's methods, required (a signature) or provided (with a body).
(trait_item
  body: (declaration_list
    [(function_item
       name: (identifier) @name)
     (function_signature_item
       name: (identifier) @name)] @definition.method))

; Functions elsewhere: in a file, in a mod, nested in a function.
(source_file
  (function_item
    name: (identifier) @name) @definition.function)

(mod_item
  body: (declaration_list
    (function_item
      name: (identifier) @name) @definition.function))

(block
  (function_item
    name: (identifier) @name) @definition.function)

; A struct's (or a union's, or an enum variant's) named fields, and enum variants.
(field_declaration_list
  (field_declaration
    name: (field_identifier) @name) @definition.field)

(enum_variant
  name: (identifier) @name) @definition.field

; Constants and statics of a file or a mod.
(source_file
  [(const_item
     name: (identifier) @name)
   (static_item
     name: (identifier) @name)] @definition.variable)

(mod_item
  body: (declaration_list
    [(const_item
       name: (identifier) @name)
     (static_item
       name: (identifier) @name)] @definition.variable))
