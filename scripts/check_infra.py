"""Verify the local infrastructure started by `make up` is healthy and complete.

Checks postgres extensions (pgvector >= 0.8.7, pg_trgm, pg_textsearch) in both databases,
redis connectivity, and that the S3 buckets exist. Exits non-zero on any failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "infra" / "docker-compose.yml"),
    *(["--env-file", str(ROOT / ".env")] if (ROOT / ".env").exists() else []),
]
MIN_PGVECTOR = (0, 8, 7)
DATABASES = ("sensei", "sensei_test")
BUCKETS = ("sensei-snapshots", "sensei-artifacts")
PG_USER = os.environ.get("SENSEI_PG_USER", "sensei")


def compose(*args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [*COMPOSE, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def compose_exec(service: str, *command: str) -> str:
    return compose("exec", "-T", service, *command)


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def check_postgres() -> list[str]:
    problems: list[str] = []
    query = "SELECT extname || '=' || extversion FROM pg_extension ORDER BY 1"
    for database in DATABASES:
        rows = compose_exec("postgres", "psql", "-U", PG_USER, "-d", database, "-tAc", query)
        extensions = dict(line.split("=", 1) for line in rows.splitlines() if "=" in line)
        for name in ("vector", "pg_trgm", "pg_textsearch"):
            if name not in extensions:
                problems.append(f"{database}: extension {name} missing")
        vector = extensions.get("vector")
        if vector and version_tuple(vector) < MIN_PGVECTOR:
            problems.append(f"{database}: pgvector {vector} is older than 0.8.7")
        print(f"  postgres/{database}: {extensions}")
    return problems


def check_redis() -> list[str]:
    reply = compose_exec("redis", "redis-cli", "ping")
    print(f"  redis: {reply}")
    return [] if reply == "PONG" else [f"redis ping returned {reply!r}"]


def check_s3() -> list[str]:
    listing = compose(
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "aws",
        "s3-init",
        "--endpoint-url",
        "http://s3:8333",
        "s3",
        "ls",
    )
    names = [line.split()[-1] for line in listing.splitlines() if line.strip()]
    print(f"  s3 buckets: {names}")
    return [f"s3 bucket {bucket} missing" for bucket in BUCKETS if bucket not in names]


def main() -> int:
    problems = check_postgres() + check_redis() + check_s3()
    if problems:
        print("infra check FAILED:")
        for problem in problems:
            print(f"  ✗ {problem}")
        return 1
    print("infra check passed ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
