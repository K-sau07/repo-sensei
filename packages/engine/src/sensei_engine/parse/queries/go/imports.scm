; Go imports, and the file's package. @source: the import path, unquoted (net/http); @alias: the
; name it is bound to when one is given. A blank import (_ "github.com/lib/pq") only runs the
; package's init; a dot import puts its names in the file's scope, as an on-demand import does.

(import_spec
  name: (blank_identifier)
  path: (_ (interpreted_string_literal_content) @source)) @import.blank

(import_spec
  name: (dot)
  path: (_ (interpreted_string_literal_content) @source)) @import.dot

(import_spec
  name: (package_identifier) @alias
  path: (_ (interpreted_string_literal_content) @source)) @import

(import_spec
  !name
  path: (_ (interpreted_string_literal_content) @source)) @import

; package handlers (@name: the package, what the file's names are reached through)
(package_clause
  (package_identifier) @name) @import.package
