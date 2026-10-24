; Imports. @source: the module (a dotted name, or a relative import with its dots); @name: the
; imported name (from-imports); @alias: the local name given with "as".

(import_statement
  name: (dotted_name) @source) @import

(import_statement
  name: (aliased_import
    name: (dotted_name) @source
    alias: (identifier) @alias)) @import

(import_from_statement
  module_name: (_) @source
  name: (dotted_name) @name) @import.from

(import_from_statement
  module_name: (_) @source
  name: (aliased_import
    name: (dotted_name) @name
    alias: (identifier) @alias)) @import.from

(import_from_statement
  module_name: (_) @source
  (wildcard_import) @name) @import.wildcard

(future_import_statement
  name: (dotted_name) @name) @import.future
