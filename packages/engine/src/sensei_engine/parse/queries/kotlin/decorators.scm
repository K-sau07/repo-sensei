; Annotations (route and injection ones among them: @GetMapping("/{id}"), @Autowired), on
; classes, functions, properties, constructor parameters and parameters. @callee: the
; annotation's name (Entity, org.springframework.lang.Nullable); @arguments: its argument list,
; when it has one.

(annotation
  (constructor_invocation
    (user_type) @callee
    (value_arguments) @arguments)) @decorator

(annotation
  (user_type) @callee) @decorator
