; String literals with their positions (URL and route extraction). A text block ("""...""") is
; a string literal too, told apart for the multi-line SQL and JSON it usually holds.

((string_literal) @string.text_block
  (#match? @string.text_block "^\"\"\""))

(string_literal) @string
