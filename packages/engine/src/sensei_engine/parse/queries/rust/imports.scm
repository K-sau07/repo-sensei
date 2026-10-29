; Rust use declarations, and the modules a file declares. @source: the path a name is imported
; from (std::sync in use std::sync::Arc; in a group, the group's path: extract in
; use axum::{extract::{Path, State}}, the enclosing groups' paths up the tree before it);
; @name: the imported name; @alias: the name given with "as". The main capture is the use
; declaration, or the group or path the name is in.

(use_declaration
  argument: (scoped_identifier
    path: (_) @source
    name: (identifier) @name)) @import

(use_as_clause
  path: (scoped_identifier
    path: (_) @source
    name: (identifier) @name)
  alias: (identifier) @alias) @import

(use_list
  (scoped_identifier
    path: (_) @source
    name: (identifier) @name) @import)

(scoped_use_list
  path: (_) @source
  list: (use_list
    [(identifier) @name
     (use_as_clause
       path: (identifier) @name
       alias: (identifier) @alias)])) @import

(use_wildcard
  .
  (_) @source
  "*" @name) @import.wildcard

; mod models; (the file models.rs or models/mod.rs, bound as models)
(mod_item
  name: (identifier) @name
  !body) @import.mod
