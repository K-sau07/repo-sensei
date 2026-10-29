; PHP use declarations, and the file's namespace. @source: the namespace a name is imported from
; (App\Models in use App\Models\Order, and a group's: use App\Services\{OrderService, ...});
; @name: the imported name; @alias: the name given with "as". use function and use const import
; a function and a constant.

(namespace_use_clause
  type: "function"
  (qualified_name
    prefix: (namespace_name) @source
    (name) @name)) @import.function

(namespace_use_clause
  type: "const"
  (qualified_name
    prefix: (namespace_name) @source
    (name) @name)) @import.const

(namespace_use_clause
  (qualified_name
    prefix: (namespace_name) @source
    (name) @name)
  alias: (name) @alias) @import

(namespace_use_clause
  (qualified_name
    prefix: (namespace_name) @source
    (name) @name)
  !alias) @import

; A group's names, and a global class (use Exception;).
(namespace_use_declaration
  (namespace_name) @source
  body: (namespace_use_group
    (namespace_use_clause
      (name) @name
      alias: (name) @alias) @import))

(namespace_use_declaration
  (namespace_name) @source
  body: (namespace_use_group
    (namespace_use_clause
      (name) @name
      !alias) @import))

(namespace_use_declaration
  (namespace_use_clause
    (name) @name
    !alias) @import)

; require, include and their _once forms, of a file named by a string literal.
(require_expression
  [(string) (encapsed_string)] @source) @import.require

(require_once_expression
  [(string) (encapsed_string)] @source) @import.require

(include_expression
  [(string) (encapsed_string)] @source) @import.require

(include_once_expression
  [(string) (encapsed_string)] @source) @import.require

; namespace App\Http\Controllers (@name: what the file's classes are imported from with use)
(namespace_definition
  name: (namespace_name) @name) @import.package
