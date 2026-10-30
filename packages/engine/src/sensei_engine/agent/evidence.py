"""The evidence an answer is written from (4.4): what the agent committed to
(``submit_evidence``), then the best of everything else the run read, reranked against the
question (the reranker's agent mode: cross-encoder, then listwise), as citable blocks for the
answer turn, within :data:`FINAL_TOKENS`.

What the agent submitted comes first, in its order: it is what the answer means to cite (each
piece cut to fit, its first lines kept, rather than left out).
The rest is the run's reading (round 0's results, every search result a tool gave), each piece
once: a piece within another of the same file is that one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sensei_engine.agent.tools.base import (
    Evidence,
    Results,
    Source,
    ToolContext,
    ToolError,
    code_result,
    file_lines,
    lines_within,
)
from sensei_engine.llm.anthropic_client import LLMUsageSink
from sensei_engine.search.rerank import Candidate, Reranker, RerankMode, RerankUsageSink

FINAL_TOKENS = 12_000
MAX_PIECES = 16
MAX_PIECE_LINES = 80  # a longer piece shows its first lines
TITLE_TOKENS = 100  # a block's source and title, kept back when its lines are cut to fit
INTRO = (
    "The evidence for your answer: first what you committed to, then the best of the rest of "
    "what you read. Answer the question from it, and cite it."
)


@dataclass(frozen=True, slots=True)
class Piece:
    path: str
    start_line: int
    end_line: int
    symbol_key: str | None = None
    why: str | None = None  # the agent's, for a piece it submitted
    layer: str | None = None

    @property
    def submitted(self) -> bool:
        return self.why is not None

    def holds(self, other: Piece) -> bool:
        return (
            self.path == other.path
            and self.start_line <= other.start_line
            and other.end_line <= self.end_line
        )


def gather(evidence: Sequence[Evidence], read: Sequence[Source]) -> list[Piece]:
    """The submitted pieces in order, then what was read in the order it was read, each piece
    once (one within an earlier piece left out)."""
    pieces: list[Piece] = []
    for found in evidence:
        pieces.append(
            Piece(found.path, found.start_line, found.end_line, None, found.why, found.layer)
        )
    for source in read:
        piece = Piece(source.path, source.start_line, source.end_line, source.symbol_key)
        if not any(held.holds(piece) for held in pieces):
            pieces.append(piece)
    return pieces


async def final_set(
    ctx: ToolContext,
    question: str,
    pieces: Sequence[Piece],
    reranker: Reranker | None,
    *,
    on_rerank_usage: RerankUsageSink | None = None,
    on_llm_usage: LLMUsageSink | None = None,
) -> list[Piece]:
    """The submitted pieces, then the rest best first (reranked; in reading order without a
    reranker), at most :data:`MAX_PIECES`."""
    submitted = [piece for piece in pieces if piece.submitted]
    rest = [piece for piece in pieces if not piece.submitted]
    if reranker is not None and len(rest) > 1:
        candidates = [
            Candidate(str(n), "\n".join(_lines(ctx, piece)), piece.path, piece.layer)
            for n, piece in enumerate(rest)
        ]
        ranking = await reranker.rerank(
            question,
            candidates,
            RerankMode.AGENT,
            on_rerank_usage=on_rerank_usage,
            on_llm_usage=on_llm_usage,
        )
        rest = [rest[int(item.candidate.id)] for item in ranking.items]
    return [*submitted, *rest][:MAX_PIECES]


def answer_blocks(
    ctx: ToolContext, pieces: Sequence[Piece]
) -> tuple[list[dict[str, Any]], list[Source], list[Piece]]:
    """The answer turn's evidence: an introduction, then each piece as a citable block (a
    submitted one titled with why it was), cut to fit what is left of :data:`FINAL_TOKENS`
    (its first lines kept), until no more fits; and the pieces shown."""
    out = Results(FINAL_TOKENS)
    out.text(INTRO)
    shown: list[Piece] = []
    for piece in pieces:
        lines = _lines(ctx, piece)
        if not lines:
            continue
        fitting = lines_within(lines, out.left - TITLE_TOKENS)
        if fitting == 0:
            out.left_out += len(pieces) - len(shown)
            break
        title = f"{piece.path}: {piece.why}" if piece.why else piece.path
        block = code_result(
            ctx,
            piece.path,
            piece.start_line,
            lines[:fitting],
            title=title,
            symbol_key=piece.symbol_key,
        )
        if out.code(*block):
            shown.append(piece)
    result = out.done("", hits=len(out.sources), narrow="cite what is here")
    return result.content, result.sources, shown


def _lines(ctx: ToolContext, piece: Piece) -> list[str]:
    try:
        lines = file_lines(ctx, piece.path, example="")
    except ToolError:
        return []
    end = min(piece.end_line, piece.start_line + MAX_PIECE_LINES - 1, len(lines))
    return lines[piece.start_line - 1 : end]
