"""Verification (4.5): claims and the names in them, citations that point at nothing, invented
identifiers caught, the support and completeness checks, and the search an incomplete answer
gets (bounded)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_engine.agent.loop import AskAgent, Done, GapFound, Verified
from sensei_engine.agent.tools import ToolContext
from sensei_engine.agent.tools.base import Cited
from sensei_engine.agent.verify import (
    MAX_ROUNDS,
    _marked,  # pyright: ignore[reportPrivateUsage]
    agreement,
    claims,
    existing,
    identifiers,
    prose,
    spliced,
    unknown,
)
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


def test_a_dotted_name_a_path_or_a_version_is_no_sentence_end() -> None:
    text = (
        "Items are created by `crud.create_item` in backend/app/crud.py. "
        "ItemsService.createItem runs on submit, e.g. from the form. It needs Python 3.10."
    )
    assert [c.text for c in claims(text, [])] == [
        "Items are created by `crud.create_item` in backend/app/crud.py.",
        "ItemsService.createItem runs on submit, e.g. from the form.",
        "It needs Python 3.10.",
    ]
    started = time.monotonic()
    assert len(claims("x" * 100_000, [])) == 1  # one pass: linear in the text
    assert time.monotonic() - started < 1


def test_names_in_code_with_arguments_count_and_products_names_do_not() -> None:
    text = (
        "TypeScript and FastAPI on iOS call `crud.create_itme(session=session, item_in=item)`, "
        "`ItemServise.make(x)` and `List<Invoyce>` with `'not_a_name'`."
    )
    assert identifiers(text) == [
        "crud", "create_itme", "session", "item_in", "item", "ItemServise", "make", "List",
        "Invoyce",
    ]  # fmt: skip


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

    def __init__(
        self,
        turns: list[dict[str, Any]],
        *,
        complete: bool,
        verdicts: list[str] | None = None,
        rewritten: str = "",
    ) -> None:
        self.turns = turns
        self.complete = complete
        self.verdicts = verdicts or ["supported"]  # the support checks', in turn (the last stays)
        self.rewritten = rewritten
        self.checks: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            turn = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
            return sse(turn)
        system = body["system"][0]["text"]
        if "against the code it cites" in system:
            self.checks.append("support")
            verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
            if verdict == "fail":  # the check's request fails
                error = {
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "x"},
                }
                return httpx2.Response(400, json=error)
            verdicts = {"claims": [{"n": 1, "verdict": verdict}]}
            return httpx2.Response(200, json=message(json.dumps(verdicts)))
        if "You correct sentences" in system:
            self.checks.append("rewrite")
            fixed = {"claims": [{"n": 1, "text": self.rewritten}]}
            return httpx2.Response(200, json=message(json.dumps(fixed)))
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
    # The checks' usage is the run's too (its budget counts it): two turns, two checks.
    assert len(done.answer.usage) == 4
    sure = done.answer.confidence
    assert sure is not None
    assert (sure.level, sure.breakdown.claims_verified, sure.breakdown.inferred) == ("high", 1, 0)
    assert done.answer.abstained is None


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
    # The gap rounds are within their own budget of turns, past the run's.
    assert done.answer.turns <= agent.budget.max_turns + agent.budget.max_gap_turns


async def test_a_gap_search_cut_short_keeps_the_answer_it_had(ctx: ToolContext) -> None:
    refused = _streamed([{"type": "text", "text": ""}], "refusal")
    model = _Model(
        [_streamed([SUBMIT], "tool_use"), _streamed([ANSWER], "end_turn"), refused],
        complete=False,
    )
    fake = FakeAnthropic(routes={"/v1/messages": model})
    agent = AskAgent(fake.client(), pre_retrieval=False)
    events = [e async for e in agent.run("where is an item saved?", ctx, overview="o")]
    done = events[-1]
    assert isinstance(done, Done)
    assert done.answer.text == "create_item adds the item to the session."
    assert len([e for e in events if isinstance(e, GapFound)]) == 1


async def test_a_claim_its_lines_show_only_part_of_is_rewritten_and_checked_again(
    ctx: ToolContext,
) -> None:
    model = _Model(
        [_streamed([SUBMIT], "tool_use"), _streamed([ANSWER], "end_turn")],
        complete=True,
        verdicts=["partial", "supported"],
        rewritten="create_item adds the item.",
    )
    fake = FakeAnthropic(routes={"/v1/messages": model})
    events = [
        e
        async for e in AskAgent(fake.client(), pre_retrieval=False).run(
            "where is an item saved?", ctx, overview="o"
        )
    ]
    done = events[-1]
    assert isinstance(done, Done)
    assert done.answer.text == "create_item adds the item."
    (citation,) = done.answer.citations
    assert (citation.start, citation.end) == (0, len(done.answer.text))  # it moved with it
    check = done.answer.verification
    assert check is not None
    assert [(c.claim.text, c.verdict) for c in check.claims] == [
        ("create_item adds the item.", "supported")
    ]
    assert model.checks == ["support", "whole", "rewrite", "support"]
    assert len([e for e in events if isinstance(e, Verified)]) == 2


def test_a_rewritten_claim_is_spliced_in_and_the_citations_move_with_the_text() -> None:
    text = "It saves the item. It only has two models. Then it commits."
    found = claims(text, [])
    first, second, third = found
    spans = [(0, 18, "a"), (19, 42, "b"), (43, 59, "c")]  # one per sentence
    out, moved, _ = spliced(text, spans, found, {second.n: "It has two models."})
    assert out == "It saves the item. It has two models. Then it commits."
    assert [(out[a:b], what) for a, b, what in moved] == [
        ("It saves the item.", "a"), ("It has two models.", "b"), ("Then it commits.", "c"),
    ]  # fmt: skip
    # A claim its lines show nothing of goes, with its space, and its citation with it.
    out, moved, _ = spliced(text, spans, found, {second.n: ""})
    assert out == "It saves the item. Then it commits."
    assert [(out[a:b], what) for a, b, what in moved] == [
        ("It saves the item.", "a"), ("Then it commits.", "c"),
    ]  # fmt: skip
    assert (first.n, third.n) == (1, 3)


def test_a_list_line_taken_out_leaves_no_empty_line() -> None:
    text = "- Saves it.\n- Only two models.\n- Commits."
    found = claims(text, [])
    gone = next(c for c in found if "models" in c.text)
    none: list[tuple[int, int, str]] = []
    out, _, _ = spliced(text, none, found, {gone.n: ""})
    assert out == "- Saves it.\n- Commits."


def test_a_rewritten_list_line_keeps_its_marker() -> None:
    assert _marked("- **Dummy hash:** it says more.", "**Dummy hash:** it says this.") == (
        "- **Dummy hash:** it says this."
    )
    assert _marked("2. Then it saves.", "2. Then it adds.") == "2. Then it adds."
    assert _marked("It saves.", "It adds.") == "It adds."
    assert _marked("- Gone.", "") == ""


def test_a_citation_starting_in_the_space_a_removal_takes_stays_with_its_claim() -> None:
    text = "It saves it. Only two. Then it commits."
    found = claims(text, [])
    gone = next(c for c in found if c.text == "Only two.")
    # The next claim's citation starts in the space after the claim taken out.
    out, moved, _ = spliced(text, [(22, 39, "c")], found, {gone.n: ""})
    assert out == "It saves it. Then it commits."
    assert [(out[a:b], what) for a, b, what in moved] == [("Then it commits.", "c")]


def test_a_citation_across_two_claims_keeps_covering_both() -> None:
    text = "It saves it. It commits."
    found = claims(text, [])
    first, second = found
    across = [(0, len(text), "x")]
    out, moved, regions = spliced(text, across, found, {second.n: "It flushes."})
    assert out == "It saves it. It flushes."
    assert [(out[a:b], what) for a, b, what in moved] == [("It saves it. It flushes.", "x")]
    assert [out[a:b] for a, b in regions] == ["It flushes."]
    out, moved, _ = spliced(text, across, found, {first.n: "It adds it.", second.n: "It flushes."})
    assert [(out[a:b], what) for a, b, what in moved] == [("It adds it. It flushes.", "x")]


def test_a_removal_keeps_a_list_line_and_leaves_no_trailing_space() -> None:
    text = "- **Label:** Only two. It saves it.\n- Next.\nIt adds. Only that."
    found = claims(text, [])
    label = next(c for c in found if c.text.startswith("- **Label:**"))
    last = next(c for c in found if c.text == "Only that.")
    none: list[tuple[int, int, str]] = []
    out, _, _ = spliced(text, none, found, {label.n: "", last.n: ""})
    assert out == "- It saves it.\n- Next.\nIt adds."


def test_code_and_table_lines_are_not_prose_to_rewrite() -> None:
    text = "It runs:\n```python\nsave(item)\n```\n| a | b |\n    indented()\nThen it ends."
    found = {c.text: c for c in claims(text, [])}
    assert prose(text, found["It runs:"])
    assert not prose(text, found["```python"])
    assert not prose(text, found["save(item)"])
    assert not prose(text, found["| a | b |"])
    assert not prose(text, found["indented()"])
    assert prose(text, found["Then it ends."])


async def test_a_failed_check_of_the_rewrite_keeps_the_answer_as_it_was_verified(
    ctx: ToolContext,
) -> None:
    model = _Model(
        [_streamed([SUBMIT], "tool_use"), _streamed([ANSWER], "end_turn")],
        complete=True,
        verdicts=["partial", "fail"],
        rewritten="create_item adds the item.",
    )
    fake = FakeAnthropic(routes={"/v1/messages": model})
    events = [
        e
        async for e in AskAgent(fake.client(), pre_retrieval=False).run(
            "where is an item saved?", ctx, overview="o"
        )
    ]
    done = events[-1]
    assert isinstance(done, Done)
    assert done.answer.text == ANSWER["text"]  # not the unchecked rewrite
    check = done.answer.verification
    assert check is not None
    assert [c.verdict for c in check.claims] == ["partial"]
    assert model.checks == ["support", "whole", "rewrite", "support"]


async def test_a_later_answer_cut_short_is_not_rewritten_by_the_checks_of_the_one_before(
    ctx: ToolContext,
) -> None:
    cut = _streamed([{"type": "text", "text": "create_item adds it."}], "max_tokens")
    model = _Model(
        # The first answer, a gap search, and the answer after it cut short.
        [
            _streamed([SUBMIT], "tool_use"),
            _streamed([ANSWER], "end_turn"),
            _streamed([SUBMIT], "tool_use"),
            cut,
        ],
        complete=False,
        verdicts=["partial"],
        rewritten="create_item adds the item.",
    )
    fake = FakeAnthropic(routes={"/v1/messages": model})
    events = [
        e
        async for e in AskAgent(fake.client(), pre_retrieval=False).run(
            "where is an item saved?", ctx, overview="o"
        )
    ]
    done = events[-1]
    assert isinstance(done, Done)
    assert done.answer.text == "create_item adds it."
    assert done.answer.verification is None  # its checks were of another text
    assert "rewrite" not in model.checks


def test_a_citation_of_a_list_claim_taken_out_goes_with_it() -> None:
    text = "- Alpha does X. Beta does Y."
    found = claims(text, [])
    alpha = next(c for c in found if "Alpha" in c.text)
    spans = [(0, 15, "a"), (16, len(text), "b")]
    out, moved, _ = spliced(text, spans, found, {alpha.n: ""})
    assert out == "- Beta does Y."
    assert [(out[a:b], what) for a, b, what in moved] == [("Beta does Y.", "b")]


def test_a_fence_closes_only_on_its_like() -> None:
    text = "Code:\n````\n```\ninner()\n````\nAfter it."
    found = {c.text: c for c in claims(text, [])}
    assert not prose(text, found["inner()"])  # the ``` inside doesn't close the ````
    assert prose(text, found["After it."])
