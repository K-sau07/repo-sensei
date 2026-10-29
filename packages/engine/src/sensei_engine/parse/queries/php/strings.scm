; String literals with their positions (URL and route extraction). A double-quoted string can
; interpolate variables; heredocs and nowdocs are told apart for the multi-line SQL they
; usually hold.

[(heredoc) (nowdoc)] @string.heredoc

(encapsed_string) @string.interpolated

(string) @string
