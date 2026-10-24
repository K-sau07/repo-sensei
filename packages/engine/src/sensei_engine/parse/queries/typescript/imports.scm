; TypeScript imports, before the shared ones (ecma/imports.scm): import fs = require("fs")

(import_statement
  (import_require_clause
    (identifier) @default
    source: (string) @source)) @import.require

(import_statement
  (import_require_clause
    source: (string) @source)) @import.module
