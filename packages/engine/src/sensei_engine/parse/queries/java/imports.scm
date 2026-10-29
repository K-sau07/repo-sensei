; Java imports, and the file's package. @source: what the name is imported from (the package
; in import a.b.C, the class in import static a.b.C.m or import a.b.Outer.Inner); @name: the
; imported name (the asterisk of an on-demand import). A static import is captured by the
; non-static patterns too, with the same roles; listed first, its kind wins.

(import_declaration
  "static"
  (scoped_identifier) @source
  (asterisk) @name) @import.static.wildcard

(import_declaration
  (scoped_identifier) @source
  (asterisk) @name) @import.wildcard

; The anchor: the name is the declaration's last part (not the package of an on-demand import).
(import_declaration
  "static"
  (scoped_identifier
    scope: (_) @source
    name: (identifier) @name) .) @import.static

(import_declaration
  (scoped_identifier
    scope: (_) @source
    name: (identifier) @name) .) @import

; package com.acme.shop; (@name: the package, what the file's classes are imported from)
(package_declaration
  [(scoped_identifier) (identifier)] @name) @import.package
