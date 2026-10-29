; Attributes (route, filter and mapping ones among them: [HttpGet("{id}")], [Authorize],
; [Table("orders")]), one per attribute ([Key, Column("id")]: two), on types, members and
; parameters. @callee: the attribute's name (HttpGet, System.Obsolete); @arguments: its argument
; list, when it has one.

(attribute
  name: (_) @callee
  (attribute_argument_list) @arguments) @decorator

(attribute
  name: (_) @callee .) @decorator
