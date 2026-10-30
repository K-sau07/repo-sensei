"""The Ask agent's events as the AI SDK's UI Message Stream (protocol v1), what ``useChat``
reads: a ``start`` with the message's id, then per model turn a ``start-step`` and its
``finish-step``; the answer's text as ``text-start``/``text-delta``/``text-end`` (a research
turn's text as reasoning: it isn't the answer); each tool call as ``tool-input-start`` and
``tool-input-available``, its result as ``tool-output-available``; the citations, the run's
progress, the confidence, what it searched and an abstention as ``data-*`` parts; ``finish``,
and the stream's ``[DONE]``. A failure is an ``error`` part, then ``finish``.

:class:`UIStream` also assembles the message as the client will hold it once the stream is
read (its ``parts``), for the conversation's history.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from sensei_engine.agent.confidence import Confidence
from sensei_engine.agent.loop import (
    AgentEvent,
    Answer,
    AnswerCitation,
    CitationFound,
    Done,
    EvidenceChosen,
    GapFound,
    PreRetrieved,
    StepStarted,
    TextDelta,
    ToolAnswered,
    ToolCalled,
    Verified,
)
from sensei_engine.agent.verify import Verification

HEADER = ("x-vercel-ai-ui-message-stream", "v1")
DONE = "[DONE]"
Part = dict[str, Any]


class UIStream:
    """One answer's events, encoded as they come (:meth:`encode`)."""

    def __init__(self, message_id: str, repo: str) -> None:
        self.message_id = message_id
        self.repo = repo
        self.parts: list[Part] = []  # the message's, as the client assembles them
        self._step = 0
        self._tools = False  # whether the open step may call tools (its text isn't the answer)
        self._open: str | None = None  # the open text (or reasoning) part's id
        self._text: list[str] = []
        self._calls: dict[str, Part] = {}
        self._verification: Verification | None = None

    def start(self) -> list[Part]:
        return [{"type": "start", "messageId": self.message_id}]

    def encode(self, event: AgentEvent) -> list[Part]:
        """The parts for ``event``: whatever isn't more text closes the text before it."""
        if isinstance(event, TextDelta):
            return self._delta(event.text)
        return [*self._close(), *self._parts(event)]

    def _parts(self, event: AgentEvent) -> list[Part]:  # noqa: PLR0911 - one case per event
        match event:
            case PreRetrieved():
                return [self._data("progress", "round0", asdict(event))]
            case StepStarted():
                return self._step_started(event)
            case TextDelta(text=text):
                return self._delta(text)
            case ToolCalled():
                return self._called(event)
            case ToolAnswered():
                return self._answered(event)
            case EvidenceChosen():
                return [self._data("progress", "evidence", asdict(event))]
            case Verified(verification=verification):
                self._verification = verification
                return [self._data("progress", "verified", _verified(verification))]
            case GapFound():
                return [self._data("progress", f"gap-{self._step}", asdict(event))]
            case CitationFound(n=n, citation=citation):
                return [self._data("citation", f"citation-{n}", self._citation(n, citation))]
            case Done(answer=answer):
                return self._done(answer)

    def failed(self, text: str) -> list[Part]:
        """A run that ended without its answer: what went wrong, then the end."""
        return [*self._close(), {"type": "error", "errorText": text}, *self._finish()]

    # ------------------------------------------------------------ the parts

    def _step_started(self, event: StepStarted) -> list[Part]:
        out = self._close()
        if self._step:
            out.append({"type": "finish-step"})
        self._step = event.turn
        self._tools = event.tools
        out.append({"type": "start-step"})
        self.parts.append({"type": "step-start"})
        return out

    def _delta(self, text: str) -> list[Part]:
        kind = "reasoning" if self._tools else "text"
        out: list[Part] = []
        if self._open is None:
            self._open = f"{kind}-{self._step}"
            self._text = []
            out.append({"type": f"{kind}-start", "id": self._open})
        self._text.append(text)
        out.append({"type": f"{kind}-delta", "id": self._open, "delta": text})
        return out

    def _close(self) -> list[Part]:
        if self._open is None:
            return []
        kind = self._open.split("-", 1)[0]
        self.parts.append({"type": kind, "text": "".join(self._text)})
        closed = [{"type": f"{kind}-end", "id": self._open}]
        self._open = None
        return closed

    def _called(self, event: ToolCalled) -> list[Part]:
        out = self._close()
        call: Part = {
            "type": f"tool-{event.name}",
            "toolCallId": event.id,
            "state": "input-available",
            "input": event.input,
        }
        self._calls[event.id] = call
        self.parts.append(call)
        out.append({"type": "tool-input-start", "toolCallId": event.id, "toolName": event.name})
        out.append(
            {
                "type": "tool-input-available",
                "toolCallId": event.id,
                "toolName": event.name,
                "input": event.input,
            }
        )
        return out

    def _answered(self, event: ToolAnswered) -> list[Part]:
        output: Part = {
            "summary": event.summary,
            "hits": event.hits,
            "isError": event.is_error,
            "ms": event.ms,
        }
        if event.card is not None:
            output["card"] = event.card
        call = self._calls.get(event.id)
        if call is not None:
            call.update(state="output-available", output=output)
        return [{"type": "tool-output-available", "toolCallId": event.id, "output": output}]

    def _citation(self, n: int, citation: AnswerCitation) -> dict[str, Any]:
        cited = citation.cited
        return {
            "n": n,
            "repo": self.repo,
            "sha": cited.sha,
            "path": cited.path,
            "lines": [cited.start_line, cited.end_line],
            "symbolKey": cited.symbol_key,
            "span": [citation.start, citation.end],
            "provenance": "evidence",
            "verified": self._verified_citation(citation),
        }

    def _verified_citation(self, citation: AnswerCitation) -> bool:
        if self._verification is None:
            return False
        return any(
            checked.verdict == "supported" and citation.cited in checked.claim.cited
            for checked in self._verification.claims
        )

    def _done(self, answer: Answer) -> list[Part]:
        out = self._close()
        if answer.text and not any(part["type"] == "text" for part in self.parts):
            # The answer was a research turn's own words (sent as reasoning): sent as the answer.
            out += [
                {"type": "text-start", "id": "text-answer"},
                {"type": "text-delta", "id": "text-answer", "delta": answer.text},
                {"type": "text-end", "id": "text-answer"},
            ]
            self.parts.append({"type": "text", "text": answer.text})
        if answer.confidence is not None:
            out.append(self._data("confidence", "confidence", confidence_data(answer.confidence)))
        out.append(self._data("searchlog", "searchlog", {"queries": list(answer.searched)}))
        if answer.abstained is not None:
            thin = answer.abstained
            data = {
                "reason": thin.reason,
                "searched": list(thin.searched),
                "trailEnd": asdict(thin.trail_end) if thin.trail_end else None,
                "suggestion": thin.suggestion,
            }
            out.append(self._data("abstain", "abstain", data))
        return [*out, *self._finish()]

    def _finish(self) -> list[Part]:
        out: list[Part] = []
        if self._step:
            out.append({"type": "finish-step"})
            self._step = 0
        out.append({"type": "finish"})
        return out

    def _data(self, name: str, key: str, data: dict[str, Any]) -> Part:
        part = {"type": f"data-{name}", "id": key, "data": data}
        self.parts = [
            p for p in self.parts if not (p["type"] == part["type"] and p.get("id") == key)
        ]
        self.parts.append(part)
        return part


def confidence_data(sure: Confidence) -> dict[str, Any]:
    return {"level": sure.level, "score": sure.score, "breakdown": asdict(sure.breakdown)}


def _verified(verification: Verification) -> dict[str, Any]:
    whole = verification.whole
    return {
        "claims": len(verification.claims),
        "cited": verification.cited,
        "supported": verification.supported,
        "droppedCitations": verification.dropped_citations,
        "unknownIdentifiers": list(verification.unknown_identifiers),
        "complete": whole.complete if whole is not None else None,
        "rounds": verification.rounds,
    }


def text_of(parts: Sequence[Part]) -> str:
    """A message's answer text: its text parts, in order."""
    return "".join(part["text"] for part in parts if part["type"] == "text")


def replayed(message_id: str, parts: Sequence[Part]) -> list[Part]:
    """A stored message (its parts as the client keeps them) as a stream again, whole: what a
    kept answer is served as."""
    out: list[Part] = [{"type": "start", "messageId": message_id}]
    step = False
    for n, part in enumerate(parts):
        kind = part["type"]
        if kind == "step-start":
            if step:
                out.append({"type": "finish-step"})
            out.append({"type": "start-step"})
            step = True
        elif kind in ("text", "reasoning"):
            key = f"{kind}-{n}"
            out += [
                {"type": f"{kind}-start", "id": key},
                {"type": f"{kind}-delta", "id": key, "delta": part["text"]},
                {"type": f"{kind}-end", "id": key},
            ]
        elif kind.startswith("tool-"):
            call, name = part["toolCallId"], kind.removeprefix("tool-")
            out.append(
                {
                    "type": "tool-input-available",
                    "toolCallId": call,
                    "toolName": name,
                    "input": part.get("input", {}),
                }
            )
            if "output" in part:
                out.append(
                    {"type": "tool-output-available", "toolCallId": call, "output": part["output"]}
                )
        elif kind.startswith("data-"):
            out.append(dict(part))
    if step:
        out.append({"type": "finish-step"})
    out.append({"type": "finish"})
    return out
