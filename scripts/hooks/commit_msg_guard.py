"""Commit message and authorship guard.

Rules:
  1. The subject must look like ``<type>(<scope>): <step> <summary>``,
     for example ``feat(api): 0.7 app factory and health checks``. Only git's own merge and revert
     subjects are exempt.
  2. No attribution lines of any kind (co-author trailers, "generated with/by", and similar),
     anywhere in the message, including lines starting with ``#`` (``git commit -m`` keeps them).
  3. The commit author email must be the repository owner's.

Used as a ``commit-msg`` hook (``commit_msg_guard.py <message-file>``) and by the pre-push check,
which validates every commit being pushed.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from pathlib import Path

EXPECTED_AUTHOR_EMAIL = "kashyap.sau@northeastern.edu"

SCISSORS = "# ------------------------ >8 ------------------------"

SUBJECT_RE = re.compile(
    r"^(feat|fix|chore|refactor|test|docs|build|ci|perf)"
    r"\([a-z0-9-]+\): "
    r"(\d+|A)\.\d+ "
    r"\S.*$"
)
GIT_GENERATED_SUBJECT_RE = re.compile(
    r'^(Merge (branch|pull request|remote-tracking branch|tag) |Revert ")'
)

_SEP = r"[\s\-_]*"
FORBIDDEN_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"co{_SEP}authored{_SEP}by",
        rf"co{_SEP}developed{_SEP}by",
        rf"assisted{_SEP}by",
        rf"generated{_SEP}(with|by)\b",
        rf"\bai{_SEP}(generated|assisted|written)\b",
        rf"\b(made|written|created|authored){_SEP}(with|by){_SEP}"
        r"(claude|chatgpt|gpt|copilot|cursor|an?\s+ai|ai)\b",
        r"noreply@anthropic\.com",
        "\N{ROBOT FACE}",
    )
)

# Unicode dashes and odd spaces are folded so look-alike characters cannot dodge the patterns.
_DASHES = dict.fromkeys(
    map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"), "-"
)


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).translate(_DASHES)
    return re.sub(r"[ \t\u00a0\u2000-\u200b]+", " ", folded)


def message_body(raw: str) -> str:
    """Everything above git's scissors line (verbose diffs below it are not part of the message)."""
    return raw.split(SCISSORS, 1)[0].strip()


def subject_line(message: str) -> str:
    for line in message.splitlines():
        if line.strip() and not line.startswith("#"):
            return line.strip()
    return ""


def check_message(raw: str) -> list[str]:
    """Return human-readable problems with the commit message (empty list means OK)."""
    message = message_body(raw)
    subject = subject_line(message)
    if not subject:
        return ["commit message is empty"]

    problems: list[str] = []
    if not (SUBJECT_RE.match(subject) or GIT_GENERATED_SUBJECT_RE.match(subject)):
        problems.append(
            "subject must match '<type>(<scope>): <step> <summary>', "
            "e.g. 'feat(api): 0.7 app factory and health checks'"
        )

    normalized = normalize(message)
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(normalized):
            problems.append(f"attribution text is not allowed (matched {pattern.pattern!r})")
    return problems


def check_author(email: str) -> list[str]:
    if email.strip().lower() != EXPECTED_AUTHOR_EMAIL:
        return [f"commit author email must be {EXPECTED_AUTHOR_EMAIL}, got {email or 'nothing'}"]
    return []


def author_email() -> str:
    ident = subprocess.run(
        ["git", "var", "GIT_AUTHOR_IDENT"],  # noqa: S607 - git resolved from PATH
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    match = re.search(r"<([^>]+)>", ident)
    return match.group(1) if match else ""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: commit_msg_guard.py <commit-msg-file>\n")
        return 2
    raw = Path(argv[1]).read_text(encoding="utf-8")
    problems = check_message(raw) + check_author(author_email())
    if problems:
        sys.stderr.write("commit rejected:\n")
        for problem in problems:
            sys.stderr.write(f"  - {problem}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
