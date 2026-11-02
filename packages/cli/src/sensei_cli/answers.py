"""An answer's stream, read as it comes: the text so far, what the agent is doing, then the
answer (the last text sent: a rewritten answer comes again, whole, after the one it replaces),
its citations, how sure it is, or why it didn't answer.

Research turns' words come as ``reasoning`` and are not the answer; tool calls are shown as what
is being done.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from sensei_cli.render import clean

Json = dict[str, Any]
# What a tool call is shown as, and the input it names.
DOING = {
    "code_search": ("Searching", "query"),
    "grep": ("Searching for", "pattern"),
    "find_symbol": ("Finding", "name"),
    "read_code": ("Reading", "path"),
    "list_dir": ("Looking in", "path"),
    "trace_feature": ("Tracing", "entry"),
    "graph_neighbors": ("Following", "symbol"),
    "get_feature_map": ("Mapping", "feature_or_path"),
    "submit_evidence": ("Checking the evidence", ""),
}


@dataclass(slots=True)
class Answer:
    texts: dict[str, str] = field(default_factory=dict[str, str])
    order: list[str] = field(default_factory=list[str])
    citations: dict[str, Json] = field(default_factory=dict[str, Json])
    confidence: str | None = None
    abstained: Json | None = None
    error: str | None = None
    doing: str = "Thinking"

    @property
    def text(self) -> str:
        return self.texts[self.order[-1]] if self.order else ""

    @property
    def cited(self) -> list[Json]:
        return list(self.citations.values())

    def take(self, event: Json) -> None:
        kind, ident = event.get("type"), str(event.get("id"))
        data = event.get("data")
        found = cast("Json", data) if isinstance(data, dict) else {}
        if kind == "text-start":
            self.texts[ident] = ""
            self.order.append(ident)
            self.doing = "Answering"
        elif kind == "text-delta":
            self.texts[ident] = self.texts.get(ident, "") + str(event.get("delta") or "")
        elif kind == "tool-input-available":
            self.doing = _doing(str(event.get("toolName")), event.get("input"))
        elif kind == "data-citation":
            self.citations[ident] = found
        elif kind == "data-confidence":
            level = found.get("level")
            self.confidence = str(level) if level else None
        elif kind == "data-abstain":
            self.abstained = found
        elif kind == "error":
            self.error = str(event.get("errorText") or "The answer failed.")


def _doing(tool: str, given: object) -> str:
    verb, key = DOING.get(tool, ("Looking", ""))
    inputs = cast("Json", given) if isinstance(given, dict) else {}
    what = inputs.get(key)
    return f"{verb}: {clean(what)[:80]}" if what else verb
