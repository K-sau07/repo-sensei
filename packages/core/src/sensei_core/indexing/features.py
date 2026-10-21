"""What a repository can do once indexing has reached a stage.

The single source for the web app too: the enums reach it through the OpenAPI schema
(``GET /api/v1/indexing/stages``), so a feature appears in the UI exactly when its stage is done.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable

from sensei_core.models import Stage


class Feature(enum.StrEnum):
    CODE_BROWSER = "code_browser"
    GOTO_DEFINITION = "goto_definition"
    KEYWORD_SEARCH = "keyword_search"
    SEMANTIC_SEARCH = "semantic_search"
    ASK = "ask"
    WIKI = "wiki"
    GRAPH_VIEW = "graph_view"
    CALLERS = "callers"
    TRACE = "trace"
    DIAGRAMS = "diagrams"
    FIXES = "fixes"
    CHANGES = "changes"


UNLOCKS: dict[Stage, tuple[Feature, ...]] = {
    Stage.DISCOVER: (Feature.CODE_BROWSER,),
    Stage.SYMBOLS: (Feature.GOTO_DEFINITION,),
    Stage.CHUNKS: (Feature.KEYWORD_SEARCH,),
    Stage.EMBEDDINGS: (Feature.SEMANTIC_SEARCH, Feature.ASK),
    Stage.SUMMARIES: (Feature.WIKI,),
    Stage.GRAPH: (Feature.GRAPH_VIEW, Feature.CALLERS),
    Stage.LINKS: (Feature.TRACE, Feature.DIAGRAMS, Feature.FIXES, Feature.CHANGES),
}


def unlocked(completed: Iterable[Stage]) -> list[Feature]:
    """Features available once every stage in ``completed`` is done, in a stable order."""
    done = set(completed)
    return [feature for stage in Stage if stage in done for feature in UNLOCKS.get(stage, ())]
