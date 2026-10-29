; Attributes (route and validation ones among them: #[Route('/orders')], #[Get('/{id}')]), one
; per attribute (#[A, B]: two), on classes, methods, properties, constants and parameters.
; @callee: the attribute's name (Route, \Symfony\Component\Routing\Attribute\Route);
; @arguments: its argument list, when it has one.

(attribute
  [(name) (qualified_name)] @callee
  parameters: (arguments) @arguments) @decorator

(attribute
  [(name) (qualified_name)] @callee
  !parameters) @decorator
