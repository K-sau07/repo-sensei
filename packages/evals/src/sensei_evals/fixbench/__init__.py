"""FixBench (8.1): the fix pipeline judged on planted bugs.

A bench is a clean application plus bugs, each a patch that plants it, a hidden test that fails
while it is there, and the issue a user would file. The bench data stays private
(``evals/private/fixbench/``); this package is the runner:

- :mod:`.bench` reads it; :mod:`.validate` checks it (each hidden test passes on the clean app,
  fails with its bug, and the app's own suite misses the bug);
- :mod:`.run` plants each bug, hands the issue to a pipeline (:mod:`.pipelines`), and judges
  what comes back: the hidden test, the app's suite, and an anti-gaming review of the diff
  (:mod:`.review`), independent of the fixer's own checks;
- :mod:`.report` says how it went: fixed, refused (not reproduced, blocked, failed), and
  **unsafe** (a fix proposed that doesn't hold), which must be zero.
"""
