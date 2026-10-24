; Definitions in the JSX dialects (JavaScript, TSX), before the shared ones. A capitalized
; function is a component candidate: the symbol extractor keeps it a component only if it
; renders JSX.

(function_declaration
  name: (identifier) @name
  (#match? @name "^[A-Z]")) @definition.component

(variable_declarator
  name: (identifier) @name
  value: [(arrow_function) (function_expression)]
  (#match? @name "^[A-Z]")) @definition.component

; const Button = React.forwardRef(...); const Row = memo(function Row() {...})
(variable_declarator
  name: (identifier) @name
  value: (call_expression
    function: (_) @_wrapper
    arguments: (arguments . [(arrow_function) (function_expression)]))
  (#match? @name "^[A-Z]")
  (#match? @_wrapper "^(React\\.)?(memo|forwardRef)$")) @definition.component
