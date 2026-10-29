; Attributes (route and derive ones among them: #[get("/orders/{id}")], #[derive(Debug)],
; #[tokio::main]), on items, fields, variants and parameters; the attribute stands before what
; it is on. @callee: the attribute's path (get, tokio::main); @arguments: its token tree, when it
; has one. A crate's or module's inner attributes (#![...]) aren't decorators.

(attribute_item
  (attribute
    [(identifier) (scoped_identifier)] @callee
    arguments: (token_tree) @arguments)) @decorator

(attribute_item
  (attribute
    [(identifier) (scoped_identifier)] @callee
    !arguments
    !value)) @decorator

(attribute_item
  (attribute
    [(identifier) (scoped_identifier)] @callee
    value: (_) @arguments)) @decorator
