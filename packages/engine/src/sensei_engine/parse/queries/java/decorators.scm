; Annotations (route and injection ones among them: @GetMapping("/{id}"), @Autowired), on
; classes, methods, constructors, fields, parameters and enum constants. @callee: the annotation's
; name (Entity, org.springframework.lang.Nullable); @arguments: its argument list, when it has
; one (("/{id}"), (name = "orders")).

(annotation
  name: (_) @callee
  arguments: (annotation_argument_list) @arguments) @decorator

(marker_annotation
  name: (_) @callee) @decorator
