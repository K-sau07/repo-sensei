"""Pre-push guard: re-validate everything that is about to leave the machine.

For each commit being pushed it checks the author email and the message rules (same rules as the
commit-msg hook), and it rejects any change touching private paths. This catches commits that
skipped the local hooks (``--no-verify``, merges, rebases, cherry-picks).

pre-commit passes the range through ``PRE_COMMIT_FROM_REF`` / ``PRE_COMMIT_TO_REF``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from commit_msg_guard import check_author, check_message

PRIVATE_PATH_RE = re.compile(
    r"^(docs/|evals/private/)|\.private\.md$|(^|/)(\.env|AGENTS\.md|CLAUDE\.md)$"
)
ZERO_SHA = "0" * 40


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", *args],  # noqa: S607 - git resolved from PATH
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def commits_to_check(from_ref: str, to_ref: str) -> list[str]:
    if not from_ref or from_ref == ZERO_SHA:
        # New branch: everything not yet on any remote branch.
        listing = git("rev-list", to_ref, "--not", "--remotes")
    else:
        listing = git("rev-list", f"{from_ref}..{to_ref}")
    return [sha for sha in listing.split() if sha]


def problems_for_commit(sha: str) -> list[str]:
    problems: list[str] = []
    email = git("show", "-s", "--format=%ae", sha).strip()
    problems += check_author(email)
    problems += check_message(git("show", "-s", "--format=%B", sha))
    changed = git("show", "--name-only", "--format=", sha).split("\n")
    problems += [
        f"private path in commit: {path}" for path in changed if PRIVATE_PATH_RE.search(path)
    ]
    return [f"{sha[:8]}: {problem}" for problem in problems]


def main() -> int:
    from_ref = os.environ.get("PRE_COMMIT_FROM_REF", "")
    to_ref = os.environ.get("PRE_COMMIT_TO_REF", "HEAD")
    problems = [p for sha in commits_to_check(from_ref, to_ref) for p in problems_for_commit(sha)]
    if problems:
        sys.stderr.write("push rejected:\n")
        for problem in problems:
            sys.stderr.write(f"  - {problem}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
