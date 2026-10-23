; References: every identifier, told apart by where it stands. An identifier two patterns
; capture takes the first one's kind.

; obj.<name>: an attribute, not a free name.
(attribute
  attribute: (identifier) @reference.attribute)

; f(<name>=...): a keyword argument's name.
(keyword_argument
  name: (identifier) @reference.keyword)

(identifier) @reference.identifier
