; Decorators (@Get(":id"), @Component({...})). @callee: the decorator expression without its
; arguments; @arguments: the argument list, when it is called.

(decorator
  (call_expression
    function: (_) @callee
    arguments: (arguments) @arguments)) @decorator

(decorator
  [(identifier) (member_expression)] @callee) @decorator
