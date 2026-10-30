"""Verification (4.5): claims and the names in them, citations that point at nothing, invented
identifiers caught, the support and completeness checks, and the search an incomplete answer
gets (bounded)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_engine.agent.loop import AskAgent, Done, GapFound, Verified
from sensei_engine.agent.tools import ToolContext
from sensei_engine.agent.tools.base import Cited
from sensei_engine.agent.verify import MAX_ROUNDS, agreement, claims, existing, identifiers, unknown
from sensei_testing.anthropic import FakeAnthropic, message, sse
from sensei_testing.indexed import ITEMS, SERVICE, index_repo

SHA = "b" * 40


def _cited(path: str, start: int, end: int, sha: str = SHA) -> Cited:
    return Cited(path, sha, start, end, None)


def test_an_answer_is_read_as_claims_with_their_citations() -> None:
    text = "create_item saves the item. It returns it!\n- the route calls it\n"
    one = _cited(SERVICE, 1, 4)
    found = claims(text, [(0, 26, one)])
    assert [(c.n, c.text, c.cited) for c in found] == [
        (1, "create_item saves the item.", (one,)),
        (2, "It returns it!", ()),
        (3, "- the route calls it", ()),
    ]


def test_names_written_as_code_are_its_identifiers() -> None:
    text = (
        "The `create_item()` function calls `session.add`; `ItemsService.createItem` and "
        "frobnicate_widget and FooBarBaz do too, and MAX_ITEMS caps them. Item is a word, "
        "`backend/app/main.py` a path and `POST /items/` a route."
    )
    assert identifiers(text) == [
        "create_item", "session", "add", "ItemsService", "createItem",
        "frobnicate_widget", "FooBarBaz", "MAX_ITEMS",
    ]  # fmt: skip


def test_two_passes_agree_by_the_spans_they_share() -> None:
    a = [("a.py", 1), ("b.py", 5), ("c.py", 9)]
    assert agreement(a, a) == 1.0
    assert agreement(a, [("a.py", 1), ("d.py", 2)]) == 0.25
    assert agreement([], []) == 1.0
    assert agreement(a, []) == 0.0


# ---------------------------------------------------------------- on an index


@pytest.fixture
async def ctx(db_session: AsyncSession, tmp_path: Path) -> ToolContext:
    snapshot, graph = await index_repo(db_session, tmp_path, ITEMS)
    return ToolContext(
        db=db_session,
        org_id=snapshot.org_id,
        repo_id=snapshot.repo_id,
        snapshot_id=snapshot.id,
        repo="o/items",
        sha=SHA,
        root=tmp_path,
        tree=sorted(ITEMS),
        graph=graph,
    )


INVENTED = ["frobnicate_item", "ItemsServiceX", "delete_all_items", "makeWidget", "ItemRepo"]


async def test_every_invented_identifier_is_caught(ctx: ToolContext) -> None:
    """Names the repository defines, or the evidence shows, pass; invented ones never do."""
    real = ["create_item", "create_item_route"]
    shown = "1\tdef create_item(item_in):\n2\t    item = Item(title=item_in.title)"
    assert await unknown(ctx, [*real, "item_in", *INVENTED], shown) == INVENTED


async def test_a_citation_of_lines_the_file_lacks_is_no_citation(ctx: ToolContext) -> None:
    found, files = existing(
        ctx,
        [
            _cited(SERVICE, 1, 4),
            _cited(SERVICE, 3, 9),  # past its 4 lines
            _cited(SERVICE, 1, 2, sha="c" * 40),  # another commit's
            _cited("backend/gone.py", 1, 1),
        ],
    )
    assert found == [True, False, False, False]
    assert len(files[SERVICE]) == 4


# ---------------------------------------------------------------- in a run


SUBMIT = {
    "type": "tool_use",
    "id": "t1",
    "name": "submit_evidence",
    "input": {
        "candidates": [
            {"target": SERVICE, "start_line": 1, "end_line": 4, "layer": "svc", "why": "saves"}
        ]
    },
}
ANSWER = {
    "type": "text",
    "text": "create_item adds the item to the session.",
    "citations": [
        {
            "type": "search_result_location",
            "source": "s",
            "title": "t",
            "cited_text": "c",
            "search_result_index": 0,
            "start_block_index": 0,
            "end_block_index": 1,
        }
    ],
}


def _streamed(content: list[dict[str, Any]], stop_reason: str) -> dict[str, Any]:
    found = message("", stop_reason=stop_reason)
    found["content"] = content
    return found


class _Model:
    """Streams the run's turns in order; answers the checks as told."""

    def __init__(self, turns: list[dict[str, Any]], *, complete: bool) -> None:
        self.turns = turns
        self.complete = complete
        self.checks: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            turn = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
            return sse(turn)
        system = body["system"][0]["text"]
        if "against the code it cites" in system:
            self.checks.append("support")
            verdicts = {"claims": [{"n": 1, "verdict": "supported"}]}
            return httpx2.Response(200, json=message(json.dumps(verdicts)))
        self.checks.append("whole")
        whole = {
            "complete": self.complete,
            "missing": [] if self.complete else ["db"],
            "gap": "" if self.complete else "Find the table create_item writes.",
        }
        return httpx2.Response(200, json=message(json.dumps(whole)))


async def test_a_verified_answer_says_what_was_checked(ctx: ToolContext) -> None:
    model = _Model(
        [_streamed([SUBMIT], "tool_use"), _streamed([ANSWER], "end_turn")], complete=True
    )
    fake = FakeAnthropic(routes={"/v1/messages": model})
    agent = AskAgent(fake.client(), pre_retrieval=False)
    events = [e async for e in agent.run("where is an item saved?", ctx, overview="o")]
    verified = [e for e in events if isinstance(e, Verified)]
    assert len(verified) == 1
    done = events[-1]
    assert isinstance(done, Done)
    check = done.answer.verification
    assert check is not None
    assert (check.cited, check.supported, check.dropped_citations) == (1, 1, 0)
    assert check.unknown_identifiers == ()
    assert check.whole is not None
    assert check.whole.complete
    assert check.rounds == 0
    assert model.checks == ["support", "whole"]


async def test_an_answer_that_is_never_whole_is_searched_for_at_most_twice(
    ctx: ToolContext,
) -> None:
    model = _Model(
        [_streamed([SUBMIT], "tool_use"), _streamed([ANSWER], "end_turn")], complete=False
    )
    fake = FakeAnthropic(routes={"/v1/messages": model})
    agent = AskAgent(fake.client(), pre_retrieval=False)
    events = [e async for e in agent.run("where is an item saved?", ctx, overview="o")]
    gaps = [e for e in events if isinstance(e, GapFound)]
    assert len(gaps) == MAX_ROUNDS
    assert gaps[0].missing == ("db",)
    done = events[-1]
    assert isinstance(done, Done)
    assert done.answer.verification is not None
    assert done.answer.verification.rounds == MAX_ROUNDS
    assert model.checks.count("whole") == MAX_ROUNDS + 1
    # Each search for the gap starts from the note that says what is missing.
    noted = [
        b
        for b in fake.bodies("/v1/messages")
        if b.get("stream")
        and any(
            isinstance(m["content"], str) and "doesn't cover all of the question" in m["content"]
            for m in b["messages"]
        )
    ]
    assert noted
