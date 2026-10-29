; String literals with their positions (URL and route extraction). Verbatim (@"...") and raw
; ("""...""") strings are told apart for the multi-line SQL they usually hold; an interpolated
; one ($"...") holds expressions.

(verbatim_string_literal) @string.verbatim

(raw_string_literal) @string.raw

(interpolated_string_expression) @string.interpolated

(string_literal) @string
