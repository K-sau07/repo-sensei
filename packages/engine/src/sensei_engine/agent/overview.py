"""The repository in brief (4.2): what the agent knows before its first tool call, the second
block of its system prompt. The same for every question on a snapshot, so it is cached with
the instructions and the tools."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select

from sensei_core.models import NodeKind, RepoFile
from sensei_engine.agent.tools.base import ToolContext

MAX_LANGUAGES = 8
MAX_DIRECTORIES = 15
# The graph's nodes worth counting for a reader: the cross-layer ones and the UI's.
COUNTED = (
    (NodeKind.API_ENDPOINT, "endpoints"),
    (NodeKind.HTTP_CLIENT_CALL, "client requests"),
    (NodeKind.TABLE, "tables"),
    (NodeKind.COMPONENT, "components"),
    (NodeKind.HOOK, "hooks"),
)


async def repo_overview(ctx: ToolContext) -> str:
    rows = await ctx.db.execute(
        select(RepoFile.language, func.count())
        .where(RepoFile.snapshot_id == ctx.snapshot_id, RepoFile.language.is_not(None))
        .group_by(RepoFile.language)
        .order_by(func.count().desc(), RepoFile.language)
        .limit(MAX_LANGUAGES)
    )
    languages = ", ".join(f"{language} ({count})" for language, count in rows.tuples())
    top = Counter(path.split("/", 1)[0] + ("/" if "/" in path else "") for path in ctx.tree)
    directories = sorted((name for name in top if name.endswith("/")), key=lambda n: -top[n])
    lines = [
        f"Repository: {ctx.repo}, at commit {ctx.sha[:12]}.",
        f"{len(ctx.tree)} files. Languages: {languages or 'none recognised'}.",
    ]
    if directories:
        shown = directories[:MAX_DIRECTORIES]
        listed = ", ".join(f"{name} ({top[name]} files)" for name in shown)
        more = (
            f", and {len(directories) - len(shown)} more" if len(directories) > len(shown) else ""
        )
        lines.append(f"Top-level directories: {listed}{more}.")
    if ctx.graph is None:
        lines.append("The code graph isn't built yet: graph_neighbors and trace_feature can't run.")
    else:
        kinds = Counter(node.kind for node in ctx.graph.nodes)
        counted = [f"{kinds[kind.value]} {label}" for kind, label in COUNTED if kinds[kind.value]]
        lines.append(f"The code graph has {', '.join(counted) or 'no cross-layer links yet'}.")
    return "\n".join(lines)
