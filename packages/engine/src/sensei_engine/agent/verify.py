"""Verification (4.5): no unverified claim reaches the user unmarked.

* **Citations exist**: each one points at lines its file has at the commit; one that doesn't
  is dropped, and the sentence that leaned on it flagged.
* **Identifiers exist**: a name the answer writes as code (``create_item``, ``ItemsService``,
  a backticked ``Item.save()``) is one the repository defines, or one the answer's evidence
  shows; any other is flagged (an invented name).
* **Claims are supported** (the fast model, every claim in one request): each sentence that
  cites, against the lines it cites: supported, partly, or not.
* **The answer is whole** (the fast model; the standard one in deep mode): does the evidence
  answer the question, in every layer it asks about? If not, what is missing: the agent
  searches once more for it, and answers again (at most :data:`MAX_ROUNDS` times).
* **Claims are rewritten** (engine §15.4; the fast model, all in one request): a sentence its
  lines show only part of, or none of, is rewritten to say only what they show (or taken out
  when they show nothing of it), spliced into the answer (:func:`spliced`) and checked again.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal

from sqlalchemy import select

from sensei_core.models import CodeNode
from sensei_engine.agent.tools.base import Cited, ToolContext, ToolError, clip, file_lines
from sensei_engine.llm.anthropic_client import (
    AnthropicClient,
    LLMError,
    LLMRequest,
    LLMUsageSink,
    Tier,
)

MAX_ROUNDS = 2  # the searches an incomplete answer may get, after its first
MAX_CITED_LINES = 40  # of a claim's citation, as the support check reads it
Verdict = Literal["supported", "partial", "unsupported"]
# Where a sentence ends: after its stop (and the space after it), or at a line's end. A stop
# within a word (crud.py, 3.10, Item.save) is no end; nor is an abbreviation's.
_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "cf.")
_BACKTICKED = re.compile(r"`([^`\n]{1,200})`")
# Names written as code outside backticks: snake_case, CONSTANT_CASE, camelCase, PascalCase
# with two humps or more (one hump is an ordinary capitalised word).
_CODE_WORD = re.compile(
    r"\b(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
    r"|[a-z]+(?:[A-Z][a-z0-9]*)+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+)\b"
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Products and tools prose names in code's case, never a repository's identifiers.
PROPER_NAMES = frozenset(
    name.lower()
    for name in [
        "TypeScript",
        "JavaScript",
        "CoffeeScript",
        "ActionScript",
        "FastAPI",
        "PostgreSQL",
        "MySQL",
        "SQLite",
        "MongoDB",
        "GraphQL",
        "WebSocket",
        "WebSockets",
        "GitHub",
        "GitLab",
        "BitBucket",
        "JetBrains",
        "IntelliJ",
        "PyCharm",
        "WebStorm",
        "VSCode",
        "NodeJS",
        "NextJS",
        "NuxtJS",
        "ReactJS",
        "VueJS",
        "AngularJS",
        "OpenAPI",
        "OAuth",
        "DynamoDB",
        "CockroachDB",
        "ClickHouse",
        "ElasticSearch",
        "OpenSearch",
        "RabbitMQ",
        "ActiveMQ",
        "ZeroMQ",
        "PyTorch",
        "TensorFlow",
        "NumPy",
        "SciPy",
        "PyPI",
        "DevOps",
        "YouTube",
        "LinkedIn",
        "PayPal",
        "WordPress",
        "DigitalOcean",
        "CloudFlare",
        "CloudFront",
        "iOS",
        "iPadOS",
        "macOS",
        "watchOS",
        "tvOS",
        "iPhone",
        "iPad",
        "eBay",
        "SwiftUI",
        "UIKit",
        "AppKit",
        "JUnit",
        "TestNG",
        "PyTest",
        "WebKit",
        "WebAssembly",
        "WebGL",
        "WebRTC",
        "OpenAI",
        "DeepMind",
        "HuggingFace",
        "LangChain",
        "LlamaIndex",
    ]
)


@dataclass(frozen=True, slots=True)
class Claim:
    """A sentence of the answer, and the citations within it."""

    n: int
    text: str
    start: int
    end: int
    cited: tuple[Cited, ...]


@dataclass(frozen=True, slots=True)
class Checked:
    claim: Claim
    verdict: Verdict | None  # None: not checked (no citation, or the check failed)


@dataclass(frozen=True, slots=True)
class Whole:
    complete: bool
    missing: tuple[str, ...]  # the layers, or the parts of the question, not answered
    gap: str  # what to search for
    # The answer's finding is that the repository doesn't have or do what was asked (its
    # premise isn't in the code): the run abstains, showing what it searched (4.6.2).
    absent: bool = False


@dataclass(frozen=True, slots=True)
class Verification:
    claims: tuple[Checked, ...]
    dropped_citations: int  # pointing at lines that aren't there
    flagged: tuple[int, ...]  # the claims (by n) that leaned on a dropped citation
    unknown_identifiers: tuple[str, ...]
    whole: Whole | None  # None: the check didn't run
    rounds: int  # the searches an incomplete answer got
    agreement: float | None = None  # of round 0's two passes (flow, architecture)

    @property
    def supported(self) -> int:
        return sum(c.verdict == "supported" for c in self.claims)

    @property
    def cited(self) -> int:
        return sum(bool(c.claim.cited) for c in self.claims)


# ---------------------------------------------------------------- deterministic checks


def claims(text: str, spans: Sequence[tuple[int, int, Cited]]) -> list[Claim]:
    """The answer's sentences (a line of a list is one), each with the citations whose span
    falls in it. One pass over the text."""
    found: list[Claim] = []
    start = 0
    for boundary in [*_BOUNDARY.finditer(text), None]:
        end = boundary.start() if boundary is not None else len(text)
        if boundary is not None and text[:end].endswith(_ABBREVIATIONS):
            continue  # "e.g. on submit": the sentence goes on
        piece = text[start:end]
        sentence = piece.strip()
        if sentence:
            first = start + len(piece) - len(piece.lstrip())
            last = first + len(sentence)
            cited = tuple(c for a, b, c in spans if a < last and b > first)
            found.append(Claim(len(found) + 1, sentence, first, last, cited))
        start = boundary.end() if boundary is not None else end
    return found


def existing(ctx: ToolContext, cited: Sequence[Cited]) -> tuple[list[bool], dict[str, list[str]]]:
    """Whether each citation's lines are its file's at the commit, and the files read."""
    files: dict[str, list[str]] = {}
    found: list[bool] = []
    for one in cited:
        if one.path not in files:
            try:
                files[one.path] = file_lines(ctx, one.path, example="")
            except ToolError:
                files[one.path] = []
        lines = files[one.path]
        found.append(one.sha == ctx.sha and 1 <= one.start_line <= one.end_line <= len(lines))
    return found, files


def identifiers(text: str) -> list[str]:
    """The names the answer writes as code: every name in backticked code (a call's arguments
    too: ``crud.create_item(session=session)``), and snake_case, camelCase and PascalCase words
    in its prose (but products' names: TypeScript, FastAPI). Paths and routes in backticks are
    left to the citations."""
    names: list[str] = []
    for found in _BACKTICKED.finditer(text):
        inner = found.group(1).strip()
        if "/" in inner:  # a path or a route
            continue
        names += _NAME.findall(re.sub(r"(['\"]).*?\1", " ", inner))  # not a string's words
    outside = _BACKTICKED.sub(" ", text)
    names += [name for name in _CODE_WORD.findall(outside) if name.lower() not in PROPER_NAMES]
    return list(dict.fromkeys(name for name in names if len(name) > 2))


async def unknown(ctx: ToolContext, names: Sequence[str], shown: str) -> list[str]:
    """The names neither the repository defines (a symbol, a graph node) nor the evidence
    shows."""
    left = [name for name in names if not re.search(rf"\b{re.escape(name)}\b", shown)]
    if not left:
        return []
    rows = await ctx.db.execute(
        select(CodeNode.name).where(
            CodeNode.snapshot_id == ctx.snapshot_id, CodeNode.name.in_(left)
        )
    )
    defined = set(rows.scalars())
    if ctx.graph is not None:
        wanted = set(left) - defined
        defined |= {node.name for node in ctx.graph.nodes if node.name in wanted}
    return [name for name in left if name not in defined]


# ---------------------------------------------------------------- the model's checks


def _prompt(name: str) -> str:
    path = resources.files("sensei_engine.agent").joinpath("prompts", f"{name}.md")
    return path.read_text(encoding="utf-8")


SUPPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["supported", "partial", "unsupported"]},
                },
                "required": ["n", "verdict"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

WHOLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "missing": {"type": "array", "items": {"type": "string"}},
        "gap": {"type": "string"},
        "absent": {"type": "boolean"},
    },
    "required": ["complete", "missing", "gap", "absent"],
    "additionalProperties": False,
}


async def support(
    client: AnthropicClient,
    checked: Sequence[Claim],
    files: dict[str, list[str]],
    *,
    on_usage: LLMUsageSink | None = None,
) -> dict[int, Verdict]:
    """Each citing claim's verdict, against the lines it cites (one request for all)."""
    citing = [claim for claim in checked if claim.cited]
    if not citing:
        return {}
    parts: list[str] = []
    for claim in citing:
        parts.append(f"[{claim.n}] {claim.text}")
        for one in claim.cited:
            lines = files.get(one.path, [])
            end = min(one.end_line, one.start_line + MAX_CITED_LINES - 1)
            shown = "\n".join(f"{n}\t{clip(lines[n - 1])}" for n in range(one.start_line, end + 1))
            parts.append(f"  cites {one.path}:{one.start_line}-{one.end_line}\n{shown}")
    request = LLMRequest(
        "\n\n".join(parts),
        system=_prompt("verify_support.v1"),
        tier=Tier.FAST,
        max_tokens=4_000,
        effort="low",
        schema=SUPPORT_SCHEMA,
        cache_system=True,
    )
    try:
        result = await client.complete(request, on_usage=on_usage)
    except LLMError:
        return {}
    known = {claim.n for claim in citing}
    return {
        int(item["n"]): item["verdict"]
        for item in result.data.get("claims", [])
        if int(item["n"]) in known
    }


REWRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["n", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}


async def rewrite(
    client: AnthropicClient,
    checked: Sequence[Checked],
    files: dict[str, list[str]],
    *,
    on_usage: LLMUsageSink | None = None,
) -> dict[int, str]:
    """Each citing claim the support check found partial or unsupported, rewritten to say only
    what its lines show ("": they show none of it, and it goes). One request for all; nothing
    when there is nothing to rewrite or the call fails (the claims stand, marked as checked)."""
    wrong = [c for c in checked if c.verdict in ("partial", "unsupported") and c.claim.cited]
    if not wrong:
        return {}
    parts: list[str] = []
    for one in wrong:
        claim = one.claim
        parts.append(f"[{claim.n}] ({one.verdict}) {claim.text}")
        for cited in claim.cited:
            lines = files.get(cited.path, [])
            end = min(cited.end_line, cited.start_line + MAX_CITED_LINES - 1)
            shown = "\n".join(
                f"{n}\t{clip(lines[n - 1])}"
                for n in range(cited.start_line, end + 1)
                if n - 1 < len(lines)
            )
            parts.append(f"  cites {cited.path}:{cited.start_line}-{cited.end_line}\n{shown}")
    request = LLMRequest(
        "\n\n".join(parts),
        system=_prompt("verify_rewrite.v1"),
        tier=Tier.FAST,
        max_tokens=4_000,
        effort="low",
        schema=REWRITE_SCHEMA,
        cache_system=True,
    )
    try:
        result = await client.complete(request, on_usage=on_usage)
    except LLMError:
        return {}
    known = {one.claim.n for one in wrong}
    return {
        int(item["n"]): str(item["text"]).strip()
        for item in result.data.get("claims", [])
        if int(item["n"]) in known
    }


def spliced[T](
    text: str,
    spans: Sequence[tuple[int, int, T]],
    found: Sequence[Claim],
    new: Mapping[int, str],
) -> tuple[str, list[tuple[int, int, T]]]:
    """``text`` with each claim ``new`` names replaced by its new words (taken out, with the
    spaces after it, when they are empty; a line left empty goes too), and ``spans`` (offsets
    in ``text``, each with what it carries) moved to match: one within a replaced claim covers
    its new words (and goes with it, when it is taken out); any other moves with the text."""
    edits: list[tuple[int, int, str]] = []
    for claim in sorted(found, key=lambda c: c.start):
        if claim.n not in new:
            continue
        start, end, words = claim.start, claim.end, new[claim.n]
        if not words:
            while end < len(text) and text[end] in " \t":
                end += 1
            if (start == 0 or text[start - 1] == "\n") and end < len(text) and text[end] == "\n":
                end += 1
        edits.append((start, end, words))
    out: list[str] = []
    at = 0
    moved: list[tuple[int, int, int]] = []  # each edit's old start, old end, new start
    for start, end, words in edits:
        out.append(text[at:start])
        moved.append((start, end, sum(len(p) for p in out)))
        out.append(words)
        at = end
    out.append(text[at:])

    def place(offset: int) -> int:
        shift = 0
        for (_, end, new_start), (_, _, words) in zip(moved, edits, strict=True):
            if end <= offset:
                shift = new_start + len(words) - end
        return offset + shift

    kept: list[tuple[int, int, T]] = []
    for a, b, carried in spans:
        inside = next(
            (
                (new_start, words)
                for (start, end, new_start), (_, _, words) in zip(moved, edits, strict=True)
                if a < end and b > start
            ),
            None,
        )
        if inside is None:
            kept.append((place(a), place(b), carried))
        elif inside[1]:
            kept.append((inside[0], inside[0] + len(inside[1]), carried))
    return "".join(out), kept


async def whole(
    client: AnthropicClient,
    question: str,
    answer: str,
    layers: Sequence[str],
    evidence: Sequence[str],
    *,
    deep: bool = False,
    on_usage: LLMUsageSink | None = None,
) -> Whole | None:
    """Whether the answer, from its evidence, answers the question in every layer it asks
    about; and if not, what is missing. None when the check couldn't run."""
    asked = ", ".join(layers) if layers else "whichever the question needs"
    prompt = (
        f"Question: {question}\n\nLayers it asks about: {asked}\n\n"
        f"Evidence the answer was written from:\n"
        + "\n".join(f"- {e}" for e in evidence)
        + f"\n\nAnswer:\n{answer}"
    )
    request = LLMRequest(
        prompt,
        system=_prompt("verify_answer.v1"),
        tier=Tier.STANDARD if deep else Tier.FAST,
        max_tokens=2_000,
        effort="low",
        schema=WHOLE_SCHEMA,
        cache_system=True,
    )
    try:
        result = await client.complete(request, on_usage=on_usage)
    except LLMError:
        return None
    data = result.data
    return Whole(
        bool(data["complete"]),
        tuple(data["missing"]),
        str(data["gap"]),
        bool(data.get("absent", False)),
    )


def agreement(first: Sequence[tuple[str, int]], second: Sequence[tuple[str, int]]) -> float:
    """How much two retrieval passes agree: the overlap of their key spans (path, first line),
    over all of them (1: the same; 0: nothing in common)."""
    a, b = set(first), set(second)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)
