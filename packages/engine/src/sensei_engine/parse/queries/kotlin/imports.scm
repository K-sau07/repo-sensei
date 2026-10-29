; Kotlin imports, and the file's package. @name: the imported name (the asterisk of an on-demand
; import); @alias: the name given with "as"; @path: the whole imported path (the grammar has no
; node for the package alone, which is @path less its last part); @source: the package of an
; on-demand import.

(import
  (qualified_identifier) @source
  "*" @name) @import.wildcard

(import
  (qualified_identifier
    (identifier) @name .) @path
  .
  (identifier) @alias) @import

((import
   (qualified_identifier
     (identifier) @name .) @path .) @import
 (#not-match? @import "[*]$"))

; package com.acme.shop (@name: the package, what the file's declarations are imported from)
(package_header
  (qualified_identifier) @name) @import.package
