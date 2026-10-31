"""Round 0 (4.3) on a small indexed repository: the question's results grouped by layer and
file, a flow traced, all of it within its budget, and the agent's first turn starting with it
(its citations counted from there)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_engine.agent.loop import AskAgent, Done, PreRetrieved
from sensei_engine.agent.round0 import INTRO, ROUND0_TOKENS, leading, pre_retrieve
from sensei_engine.agent.tools import ToolContext
from sensei_engine.chunk.tokens import count_tokens
from sensei_engine.graph.trace import TracePath
from sensei_engine.llm.anthropic_client import LLMUsageSink
from sensei_engine.search.route import Route, classify
from sensei_engine.search.service import SearchService
from sensei_testing.anthropic import FakeAnthropic, message, sse
from sensei_testing.indexed import ITEMS, ROUTE, SERVICE, index_repo

FLOW = "what happens when an item is created, from the route to the database?"


@pytest.fixture
async def ctx(db_session: AsyncSession, tmp_path: Path) -> ToolContext:
    snapshot, graph = await index_repo(db_session, tmp_path, ITEMS)
    return ToolContext(
        db=db_session,
        org_id=snapshot.org_id,
        repo_id=snapshot.repo_id,
        snapshot_id=snapshot.id,
        repo="o/items",
        sha="b" * 40,
        root=tmp_path,
        tree=sorted(ITEMS),
        graph=graph,
    )


def _texts(content: tuple[dict[str, Any], ...]) -> list[str]:
    return [b["text"] if b["type"] == "text" else b["title"] for b in content]


async def test_round0_groups_by_layer_and_file_and_traces_a_flow(ctx: ToolContext) -> None:
    found = await pre_retrieve(ctx, FLOW)
    assert found is not None
    texts = _texts(found.content)
    assert texts[0] == INTRO
    assert texts[1].startswith("The flow, traced from its best match:\nPath 1 (")
    assert found.traced >= 1
    layers = [t for t in texts if t.startswith("Layer ")]
    assert layers == sorted(layers, key=["Layer api:", "Layer svc:", "Layer other:"].index)
    paths = [s.path for s in found.sources]
    assert ROUTE in paths
    assert SERVICE in paths
    # The api layer's results come before the svc layer's.
    assert paths.index(ROUTE) < paths.index(SERVICE)
    assert found.results == len(found.sources)
    assert found.files == len(set(paths))
    shown = sum(
        count_tokens(b["text"]) if b["type"] == "text" else count_tokens(json.dumps(b["content"]))
        for b in found.content
    )
    assert shown <= ROUND0_TOKENS


async def test_no_trace_without_a_graph_and_nothing_for_no_match(ctx: ToolContext) -> None:
    found = await pre_retrieve(dataclasses.replace(ctx, graph=None), FLOW)
    assert found is not None
    assert found.traced == 0
    assert not any(t.startswith("The flow") for t in _texts(found.content))
    assert await pre_retrieve(ctx, "zebra quantum flux") is None


async def test_the_first_turn_starts_with_round0_and_cites_it(ctx: ToolContext) -> None:
    found = await pre_retrieve(ctx, FLOW)
    assert found is not None
    first = next(n for n, s in enumerate(found.sources) if s.path == SERVICE)
    answered = message("", stop_reason="end_turn")
    answered["content"] = [
        {
            "type": "text",
            "text": "The service adds it to the session.",
            "citations": [
                {
                    "type": "search_result_location",
                    "source": "s",
                    "title": "t",
                    "cited_text": "c",
                    "search_result_index": first,
                    "start_block_index": 0,
                    "end_block_index": 1,
                }
            ],
        }
    ]

    def reply(_: httpx2.Request) -> httpx2.Response:
        return sse(answered)

    fake = FakeAnthropic(routes={"/v1/messages": reply})
    events = [e async for e in AskAgent(fake.client(), verify=False).run(FLOW, ctx, overview="o")]
    retrieved = events[0]
    assert isinstance(retrieved, PreRetrieved)
    assert retrieved.results == found.results
    # Research starts with round 0; reminded once, it submits nothing; the answer is written
    # from what round 0 found (read in its order: no reranker), and cites it.
    first_turn, _, answer_turn = fake.bodies("/v1/messages")
    content = first_turn["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": INTRO}
    assert content[-1] == {"type": "text", "text": FLOW}
    assert {b["type"] for b in content} == {"text", "search_result"}
    shown = [b for b in answer_turn["messages"][-1]["content"] if b["type"] == "search_result"]
    assert shown[first]["title"].startswith(SERVICE)
    done = events[-1]
    assert isinstance(done, Done)
    (citation,) = done.answer.citations
    assert citation.cited.path == SERVICE


class _TwoWays(SearchService):
    """A router that decomposes the question both ways (as the model's does)."""

    async def route(
        self, query: str, *, scope: str, on_llm_usage: LLMUsageSink | None = None
    ) -> Route:
        return dataclasses.replace(
            classify(query).route,
            structural_rewrite="create_item_route create_item",
            behavioural_rewrite="add the item to the session",
        )


async def test_a_flow_is_searched_both_ways_and_their_agreement_kept(ctx: ToolContext) -> None:
    found = await pre_retrieve(dataclasses.replace(ctx, search=_TwoWays()), FLOW)
    assert found is not None
    assert found.agreement is not None
    assert 0.0 <= found.agreement <= 1.0
    assert SERVICE in {s.path for s in found.sources}
    once = await pre_retrieve(ctx, FLOW)  # the rules give no decomposition: one pass
    assert once is not None
    assert once.agreement is None


def test_a_flow_is_traced_from_the_first_match_that_reaches_the_data() -> None:
    def path(reaches: bool) -> TracePath:
        return cast("TracePath", SimpleNamespace(reaches_goal=reaches, hops=[1, 2]))

    styled, saved = [path(False)], [path(True), path(False)]
    assert leading([styled, [], saved]) == saved  # the shared component's trace stops short
    assert leading([styled, [path(False)]]) == styled  # none reaches it: the best match's
    assert leading([[], []]) == ()
