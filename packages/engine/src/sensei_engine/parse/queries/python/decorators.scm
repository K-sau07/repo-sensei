; Decorators (route decorators among them: @router.get("/items/{id}")). @callee: the decorator
; expression without its arguments; @arguments: the argument list, when it is called.

(decorator
  (call
    function: (_) @callee
    arguments: (argument_list) @arguments)) @decorator

(decorator
  [(identifier) (attribute)] @callee) @decorator
