"""What tests of a fix's or a change's stages share (8.7, 9.2): a git repository of given files,
the tests' own pytest standing in for the sandbox, and a model scripted turn by turn."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from sensei_testing.anthropic import FakeAnthropic, message, sse


def git(root: Path, *args: str) -> str:
    """git's output for ``args`` in ``root`` (fails the test if git does)."""
    done = subprocess.run(  # noqa: S603 - a fixed git argv
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],  # noqa: S607
        cwd=root, check=True, capture_output=True, text=True,
    )  # fmt: skip
    return done.stdout


def git_repo(root: Path, files: dict[str, str]) -> Path:
    """A repository at ``root`` holding ``files``, committed once."""
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


@dataclass
class LocalPytest:
    """The tests' own pytest on this machine, in place of the sandbox (the files it runs are
    the test's: ours). Writes no bytecode, so the checkout keeps only what the stages wrote."""

    root: Path
    project: str = ""

    async def run(self, argv: Sequence[str]) -> Any:
        from sensei_fixer.runs import RunResult  # noqa: PLC0415 - the fixer is the test's

        cmd = [sys.executable if argv[0] == "python" else argv[0], *argv[1:]]
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *cmd, cwd=self.root / self.project, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )  # fmt: skip
        out, _ = await process.communicate()
        text, code = out.decode(), process.returncode
        return RunResult(code == 0, code, text[-6000:], time.monotonic() - started, full=text)


def tool_use(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def scripted(*turns: list[dict[str, Any]]) -> FakeAnthropic:
    """A model answering each turn with the next of ``turns`` (each a list of blocks)."""
    queue: list[dict[str, Any]] = []
    for content in turns:
        found = message("", stop_reason="tool_use")
        found["content"] = content
        queue.append(found)

    def reply(_request: httpx2.Request) -> httpx2.Response:
        return sse(queue.pop(0))

    return FakeAnthropic(routes={"/v1/messages": reply})
