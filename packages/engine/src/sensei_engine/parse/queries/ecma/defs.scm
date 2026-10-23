; Definitions shared by JavaScript, TypeScript and TSX. The main capture (@definition.<kind>)
; is the definition node, @name its name. A node two patterns capture takes the first one's
; kind, so specific patterns come first (a language's own file comes before this one).

; Hooks: functions named use<Upper> (React's rule).
(function_declaration
  name: (identifier) @name
  (#match? @name "^use[A-Z0-9]")) @definition.hook

(variable_declarator
  name: (identifier) @name
  value: [(arrow_function) (function_expression)]
  (#match? @name "^use[A-Z0-9]")) @definition.hook

(function_declaration
  name: (identifier) @name) @definition.function

(generator_function_declaration
  name: (identifier) @name) @definition.function

; const f = () => ...; const f = function () {...}
(variable_declarator
  name: (identifier) @name
  value: [(arrow_function) (function_expression) (generator_function)]) @definition.function

(class_declaration
  name: (_) @name) @definition.class

(variable_declarator
  name: (identifier) @name
  value: (class)) @definition.class

(method_definition
  name: (_) @name) @definition.method

; Module-level variables (functions and classes among them are taken above).
(program
  [(lexical_declaration (variable_declarator name: (identifier) @name) @definition.variable)
   (variable_declaration (variable_declarator name: (identifier) @name) @definition.variable)])

(program
  (export_statement
    declaration: [
      (lexical_declaration (variable_declarator name: (identifier) @name) @definition.variable)
      (variable_declaration (variable_declarator name: (identifier) @name) @definition.variable)]))

; export default () => ...; export default class {...} (no name of their own)
(export_statement
  "default"
  value: [(arrow_function) (function_expression)] @definition.function)

(export_statement
  "default"
  value: (class) @definition.class)

; Exports. @declaration: what is exported with it; @value: a default-exported expression;
; @name / @alias: a name in an export list (export { a as b }).
(export_statement
  "default"
  declaration: (_) @declaration) @export.default

(export_statement
  "default"
  value: (_) @value) @export.default

(export_statement
  declaration: (_) @declaration) @export

(export_statement
  !source
  (export_clause
    (export_specifier
      name: (_) @name
      alias: (_)? @alias))) @export
