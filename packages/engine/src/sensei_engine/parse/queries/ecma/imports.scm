; Imports. @source: the module specifier (a string); @name: an imported name, @alias its local
; name; @default / @namespace: the local name of a default or namespace import. A statement
; that names a module is also captured once as a whole (@import.module), so side-effect imports
; (import "./styles.css") and plain re-exports count too. A node two patterns capture with the
; same roles takes the first one's kind.

(import_statement
  (import_clause
    (identifier) @default)
  source: (string) @source) @import.default

(import_statement
  (import_clause
    (named_imports
      (import_specifier
        name: (_) @name
        alias: (_)? @alias)))
  source: (string) @source) @import.named

(import_statement
  (import_clause
    (namespace_import
      (identifier) @namespace))
  source: (string) @source) @import.namespace

; Re-exports: export { a as b } from "m"; export * as ns from "m"; export * from "m"
(export_statement
  (export_clause
    (export_specifier
      name: (_) @name
      alias: (_)? @alias))
  source: (string) @source) @import.reexport

(export_statement
  (namespace_export
    (_) @namespace)
  source: (string) @source) @import.reexport

(export_statement
  "*"
  source: (string) @source) @import.reexport

(import_statement
  source: (string) @source) @import.module

(export_statement
  source: (string) @source) @import.module

; require("m") and import("m")
(call_expression
  function: (identifier) @_require
  arguments: (arguments
    .
    (string) @source
    .)
  (#eq? @_require "require")) @import.require

(call_expression
  function: (import)
  arguments: (arguments
    .
    [(string) (template_string)] @source)) @import.dynamic
