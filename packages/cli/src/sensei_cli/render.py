"""What the commands print. Everything the server sends (repository text above all: code,
names, answers) is cleaned of terminal control characters first, so a repository can't move the
cursor, rewrite the screen or set the terminal's title through ``sensei``."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from rich.console import Console
from rich.table import Table
from rich.text import Text

Json = dict[str, Any]
SNIPPET_LINES = 3
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(value: object) -> str:
    """``value`` as text, its control characters shown, not sent (ESC as ``\\x1b``)."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", "" if value is None else str(value))


def items(value: Any) -> list[Any]:
    """``value`` if a list (the API's JSON, unchecked), else none."""
    return cast("list[Any]", value) if isinstance(value, list) else []


def _dicts(value: Any) -> list[Json]:
    return [cast("Json", v) for v in items(value) if isinstance(v, dict)]


def _dict(value: Any) -> Json:
    return cast("Json", value) if isinstance(value, dict) else {}


def where(path: object, lines: Sequence[object] | None) -> str:
    """``path:L10-42`` (``path:L10`` for one line; the path alone without lines)."""
    shown = clean(path)
    if not lines or len(lines) < 2:
        return shown
    start, end = lines[0], lines[1]
    return f"{shown}:L{start}" if start == end else f"{shown}:L{start}-{end}"


def _when(value: object, *, day: bool = False) -> str:
    if not isinstance(value, str):
        return "never"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d" if day else "%Y-%m-%d %H:%M UTC")
    except ValueError:
        return clean(value)


def _languages(found: object, top: int = 3) -> str:
    if not isinstance(found, dict):
        return ""
    sizes = cast("dict[str, Any]", found)
    ranked = sorted(sizes.items(), key=lambda kv: -(kv[1] if isinstance(kv[1], int) else 0))
    return ", ".join(clean(name) for name, _ in ranked[:top])


# ---------------------------------------------------------------- who, repos, index


def whoami(console: Console, me: Json, *, server: str, how: str) -> None:
    name = f" ({clean(me['name'])})" if me.get("name") else ""
    console.print(Text(f"Signed in to {server} as {clean(me.get('login'))}{name}"))
    for org in _dicts(me.get("orgs")):
        console.print(Text(f"Organization: {clean(org.get('slug'))} ({clean(org.get('role'))})"))
    console.print(Text(how))


def repos(console: Console, found: list[Json]) -> None:
    if not found:
        console.print("No repositories yet. Connect one with `sensei repos connect <url>`.")
        return
    table = Table(box=None, pad_edge=False, header_style="bold")
    # The GitHub name is in `index status` and --json: an 80-column terminal fits the rest.
    table.add_column("Repo", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Languages", no_wrap=True, overflow="ellipsis")
    table.add_column("Files", no_wrap=True, justify="right")
    table.add_column("Indexed", no_wrap=True)
    for repo in found:
        files = repo.get("file_count")
        table.add_row(
            clean(repo.get("slug")),
            _status(repo),
            _languages(repo.get("languages"), top=2),
            f"{files:,}" if isinstance(files, int) else "",
            _when(repo.get("last_indexed_at"), day=True),
        )
    console.print(table)


def _status(repo: Json) -> str:
    job = _dict(repo.get("active_job"))
    if not job:
        return clean(repo.get("status"))
    stage = f" · {clean(job.get('stage'))}" if job.get("stage") else ""
    progress = job.get("progress")
    share = f" {round(float(progress) * 100)}%" if isinstance(progress, int | float) else ""
    return f"{clean(job.get('status'))}{stage}{share}"


def _running(repo: Json) -> str:
    """Its status, and the time left when an index is running and says."""
    eta = _dict(repo.get("active_job")).get("eta_s")
    left = (
        f" · about {max(1, round(eta / 60))} min left" if isinstance(eta, int) and eta > 0 else ""
    )
    return _status(repo) + left


def progress(console: Console, repo: Json) -> None:
    """The running index's step, one line (``index status --watch``)."""
    console.print(Text(f"  {_running(repo)}"))


def index_status(console: Console, repo: Json) -> None:
    console.print(Text(f"{clean(repo.get('full_name'))} ({clean(repo.get('slug'))})", "bold"))
    console.print(Text(f"Status: {_running(repo)}"))
    sha = repo.get("last_indexed_sha")
    last = f"Last indexed: {_when(repo.get('last_indexed_at'))}"
    if isinstance(sha, str):
        last += f" at {clean(sha[:7])}"
    files, loc = repo.get("file_count"), repo.get("loc")
    if isinstance(files, int):
        last += f" · {files:,} files"
    if isinstance(loc, int):
        last += f" · {loc:,} lines"
    console.print(Text(last))


def estimate(console: Console, found: Json) -> None:
    kind = "private" if found.get("private") else "public"
    console.print(Text(f"{clean(found.get('full_name'))} · {kind}", "bold"))
    files, tokens = found.get("files"), found.get("tokens")
    minutes = found.get("index_minutes")
    parts: list[str] = []
    if isinstance(files, int):
        parts.append(f"about {files:,} files")
    if isinstance(tokens, int):
        parts.append(f"{tokens:,} tokens")
    if isinstance(minutes, int | float):
        parts.append(f"about {max(1, round(minutes))} min to index")
    console.print(Text(" · ".join(parts)))
    unpriced = [clean(u) for u in items(found.get("unpriced"))]
    if unpriced:
        console.print(Text(f"Cost: unknown (no price for {', '.join(unpriced)})"))
    else:
        cost = float(found.get("cost_usd") or 0)
        shown = "less than $0.01" if 0 < cost < 0.01 else f"about ${cost:.2f}"
        console.print(Text(f"Cost: {shown}, once"))


# ---------------------------------------------------------------- search


def best_first(found: Json, limit: int) -> list[Json]:
    hits = [h for lane in _dict(found.get("lanes")).values() for h in _dicts(lane)]
    hits.sort(key=lambda h: -float(h.get("score") or 0))
    return hits[:limit]


def search(console: Console, found: Json, limit: int) -> None:
    hits = best_first(found, limit)
    partial = _dict(found.get("partial"))
    if not hits:
        console.print(Text(f"Nothing found for {clean(found.get('query'))!r}."))
    for n, hit in enumerate(hits, 1):
        head = Text(f"{n:>2}  ")
        head.append(clean(hit.get("name")), "bold")
        head.append(f"  {clean(hit.get('kind'))} · {clean(hit.get('layer'))}", "dim")
        console.print(head)
        lines = [hit.get("start_line"), hit.get("end_line")]
        console.print(Text(f"    {where(hit.get('path'), lines)}"), overflow="fold")
        snippet = _dict(hit.get("snippet"))
        first = snippet.get("start_line")
        for i, line in enumerate(_strs(snippet.get("lines"))[:SNIPPET_LINES]):
            number = f"{first + i:>6}" if isinstance(first, int) else " " * 6
            console.print(Text(f"    {number}  {clean(line)}", "dim"), overflow="ellipsis",
                          no_wrap=True)  # fmt: skip
    if partial.get("semantic_pending"):
        console.print(Text("Search by meaning is still being prepared: these are matches by name "
                           "and text.", "italic"))  # fmt: skip


def _strs(value: Any) -> list[str]:
    return [str(v) for v in items(value)]


# ---------------------------------------------------------------- trace

LAYER_WIDTH = 7  # the longest layer's name: handler, service
HOP_INDENT = " " * (LAYER_WIDTH + 3)
# Why a path stops: the graph misses the next hop; the code goes no further (server code that
# touches no data); or the trace's hop limit (the code goes on).
STOPS = {
    "gap": ("⚠ The trail breaks here", "yellow"),
    "end": ("■ It ends here", "dim"),
    "limit": ("… Stopped at the hop limit", "dim"),
}


def trace(console: Console, found: Json, start: str) -> None:
    paths = _dicts(found.get("paths"))
    if not found.get("linked"):
        console.print(Text("The links across layers are still being built: paths may stop "
                           "early.", "italic"))  # fmt: skip
    if not paths:
        entries = _dicts(found.get("entries"))
        console.print(Text(f"No path found from {clean(start)!r}."))
        for entry in entries:
            console.print(Text(f"  starts at {clean(entry.get('qualified_name'))}  "
                               f"{where(entry.get('path'), entry.get('lines'))}"))  # fmt: skip
        return
    for n, path in enumerate(paths, 1):
        sure = path.get("path_confidence")
        head = f"Path {n}"
        if isinstance(sure, int | float):
            head += f" · {round(sure * 100)}% sure"
        if path.get("reaches_goal"):
            head += " · reaches the data"
        if n > 1:
            console.print()
        console.print(Text(head, "bold"))
        breaks: dict[object, list[Json]] = {}
        for gap in _dicts(path.get("breaks")):
            breaks.setdefault(gap.get("after_hop"), []).append(gap)
        returning = path.get("return_index")
        for i, hop in enumerate(_dicts(path.get("hops"))):
            if i == returning:
                console.print(Text(f"{HOP_INDENT}↩ back in the client", "dim"))
            _hop(console, hop, breaks.get(i, []))


def _hop(console: Console, hop: Json, stops: list[Json]) -> None:
    """A hop: the edge it is reached by, the node, where it is, and why the path stops there."""
    node, edge = _dict(hop.get("node")), _dict(hop.get("edge"))
    if edge:
        sure = edge.get("confidence")
        share = f" ({round(sure * 100)}%)" if isinstance(sure, int | float) else ""
        console.print(Text(f"{HOP_INDENT}↓ {clean(edge.get('type'))}{share}", "dim"))
    row = Text(f"  {clean(hop.get('layer')):<{LAYER_WIDTH}} ")
    row.append(clean(node.get("qualified_name")), "bold")
    row.append(f"  {clean(node.get('kind'))}", "dim")
    console.print(row)
    if node.get("path"):
        place = where(node.get("path"), node.get("lines"))
        console.print(Text(f"{HOP_INDENT}{place}", "dim"), overflow="fold")
    for gap in stops:
        mark, style = STOPS.get(str(gap.get("kind")), STOPS["gap"])
        console.print(Text(f"  {mark}: {clean(gap.get('reason'))}", style))


# ---------------------------------------------------------------- ask


def citations(console: Console, cited: list[Json]) -> None:
    if not cited:
        return
    console.print()
    console.print(Text("Sources", "bold"))
    for c in sorted(cited, key=lambda c: int(c.get("n") or 0)):
        line = Text(f"[{c.get('n')}] ")
        line.append(where(c.get("path"), c.get("lines")))
        if c.get("verified"):
            line.append("  verified", "green")
        console.print(line)


def abstained(console: Console, why: Json) -> None:
    console.print(Text(f"Not answered from the code: {clean(why.get('reason'))}", "yellow"))
    searched = [clean(s) for s in items(why.get("searched"))]
    if searched:
        console.print(Text(f"Searched: {'; '.join(searched)}", "dim"))
    if why.get("suggestion"):
        console.print(Text(f"Try: {clean(why.get('suggestion'))}"))
