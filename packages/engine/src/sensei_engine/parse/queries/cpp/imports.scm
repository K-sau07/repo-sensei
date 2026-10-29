; C++ using declarations and directives, namespace aliases, and the file's namespaces (the C
; includes are inherited). @source: what a name is imported from (std in using std::string; a
; qualified one captured a part at a time, outermost first: a, b for using a::b::c;), or the
; namespace a directive opens; @name: the imported name; @alias: the name a namespace alias
; binds. A class's using Base::f; (it brings f into the class) is captured too. A directive
; (using namespace a::b;) is only an @import.namespace.

(using_declaration
  "namespace"
  [(identifier) (qualified_identifier)] @source) @import.namespace

((using_declaration
   (qualified_identifier
     scope: (_) @source
     name: (identifier) @name)) @import
 (#not-match? @import "^using\\s+namespace\\b"))

((using_declaration
   (qualified_identifier
     scope: (_) @source
     name: (qualified_identifier
       scope: (_) @source
       name: (identifier) @name))) @import
 (#not-match? @import "^using\\s+namespace\\b"))

((using_declaration
   (qualified_identifier
     scope: (_) @source
     name: (qualified_identifier
       scope: (_) @source
       name: (qualified_identifier
         scope: (_) @source
         name: (identifier) @name)))) @import
 (#not-match? @import "^using\\s+namespace\\b"))

; namespace fs = std::filesystem;
(namespace_alias_definition
  name: (namespace_identifier) @alias
  [(namespace_identifier) (nested_namespace_specifier)] @source) @import

; namespace acme (@name: what the file's names are reached through, using namespace acme)
(namespace_definition
  name: (_) @name) @import.package
