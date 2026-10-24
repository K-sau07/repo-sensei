; JSX elements by name (<Item />, <Box.Inner>); fragments have none and aren't captured.

(jsx_opening_element
  name: (_) @name) @jsx.element

(jsx_self_closing_element
  name: (_) @name) @jsx.element
