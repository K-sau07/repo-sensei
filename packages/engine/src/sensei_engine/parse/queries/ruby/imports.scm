; Ruby requires, of a file named by a string literal. @source: what is required (json, a gem's
; path); require_relative's is a path from the requiring file's directory.

((call
   !receiver
   method: (identifier) @_method
   arguments: (argument_list
     .
     (string
       (string_content) @source) .)) @import.relative
 (#eq? @_method "require_relative"))

((call
   !receiver
   method: (identifier) @_method
   arguments: (argument_list
     .
     (string
       (string_content) @source) .)) @import.require
 (#eq? @_method "require"))
