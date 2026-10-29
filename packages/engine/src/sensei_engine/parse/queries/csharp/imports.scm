; C# using directives, and the file's namespaces. @source: what is imported (a namespace, or a
; type for using static); @alias: the name an alias directive binds (using Db = Acme.Data). A
; global using is captured as a plain one. A static using is captured by the namespace pattern
; too, with the same roles; listed first, its kind wins.

(using_directive
  "static"
  [(qualified_name) (identifier) (generic_name)] @source) @import.static

(using_directive
  name: (identifier) @alias
  [(qualified_name) (identifier) (generic_name)] @source) @import

(using_directive
  !name
  [(qualified_name) (identifier)] @source) @import.namespace

; namespace Acme.Shop (@name: what the file's types are imported from with using)
(namespace_declaration
  name: (_) @name) @import.package

(file_scoped_namespace_declaration
  name: (_) @name) @import.package
