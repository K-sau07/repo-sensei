"""The Ask agent's loop (4.2) on recorded answers: tool calls run and answered in one message,
the turn cap and the budget end in an answer, citations resolve, the prefix is cached, deep
questions escalate."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest

from sensei_core.usage import DEFAULT_PRICE_TABLE, CostCalculator
from sensei_engine.agent.evidence import (
    INTRO,
    MAX_PIECES,
    Piece,
    answer_blocks,
    final_set,
    gather,
)
from sensei_engine.agent.loop import (
    BUDGET_NOTE,
    EVIDENCE_REMINDER,
    LAST_TURN_NOTE,
    AgentEvent,
    AskAgent,
    Budget,
    Done,
    EvidenceChosen,
    Exchange,
    Mode,
    StepStarted,
    TextDelta,
    plan_for,
)
from sensei_engine.agent.tools import ToolContext
from sensei_engine.agent.tools.base import Evidence, Source
from sensei_engine.llm.anthropic_client import LLMUsageSink, Tier
from sensei_engine.search.rerank import (
    Candidate,
    Ranked,
    Ranking,
    Reranker,
    RerankMode,
    RerankUsageSink,
)
from sensei_engine.search.route import classify
from sensei_testing.anthropic import FakeAnthropic, message, sse

PATH = "/v1/messages"
OVERVIEW = "Repository: o/items, at commit bbbbbbbbbbbb.\n2 files."
SERVICE = "backend/app/services/items.py"
SERVICE_CODE = (
    "def create_item(item_in):\n"
    "    item = Item(title=item_in.title)\n"
    "    session.add(item)\n"
    "    return item\n"
)


def _ctx(root: Path) -> ToolContext:
    (root / "backend/app/services").mkdir(parents=True)
    (root / SERVICE).write_text(SERVICE_CODE)
    (root / "README.md").write_text("# Items\n")
    return ToolContext(
        db=None,  # type: ignore[arg-type]  # the tools these runs call read files only
        org_id=None,  # type: ignore[arg-type]
        repo_id=None,  # type: ignore[arg-type]
        snapshot_id=None,  # type: ignore[arg-type]
        repo="o/items",
        sha="b" * 40,
        root=root,
        tree=["README.md", SERVICE],
    )


def _answer(content: list[dict[str, Any]], stop_reason: str, **usage: int) -> dict[str, Any]:
    found = message("", stop_reason=stop_reason)
    found["content"] = content
    found["usage"] = {
        "input_tokens": 1000,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        **usage,
    }
    return found


def _tool(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def _fake(*answers: dict[str, Any]) -> FakeAnthropic:
    queue = list(answers)

    def reply(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body["max_tokens"] == 0:  # a pre-warm: not streamed
            return httpx2.Response(200, json=_answer([], "max_tokens", output_tokens=0))
        return sse(queue.pop(0))

    return FakeAnthropic(routes={PATH: reply})


async def _events(agent: AskAgent, question: str, ctx: ToolContext, **kw: Any) -> list[AgentEvent]:
    return [event async for event in agent.run(question, ctx, overview=OVERVIEW, **kw)]


READ = _tool("t1", "read_code", {"path": SERVICE})
LIST = _tool("t2", "list_dir", {})
SUBMIT = _tool(
    "t3",
    "submit_evidence",
    {
        "candidates": [
            {"target": SERVICE, "start_line": 1, "end_line": 4, "layer": "svc", "why": "saves it"}
        ]
    },
)
CITED = {
    "type": "text",
    "text": "create_item adds the item to the session.",
    "citations": [
        {
            "type": "search_result_location",
            "source": f"o/items@{'b' * 40}:{SERVICE}#L1-L4",
            "title": SERVICE,
            "cited_text": "3\t    session.add(item)",
            "search_result_index": 0,
            "start_block_index": 0,
            "end_block_index": 1,
        },
        {
            "type": "search_result_location",
            "source": "x",
            "title": "x",
            "cited_text": "x",
            "search_result_index": 7,  # nothing the answer was shown
            "start_block_index": 0,
            "end_block_index": 1,
        },
    ],
}
QUESTION = "where is an item saved?"


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _titles(body: dict[str, Any]) -> list[str]:
    return [b["title"] for b in body["messages"][-1]["content"] if b["type"] == "search_result"]


async def test_research_commits_to_evidence_and_the_answer_cites_only_it(tmp_path: Path) -> None:
    fake = _fake(
        _answer([{"type": "thinking", "thinking": "", "signature": "sig"}, READ, LIST], "tool_use"),
        _answer([SUBMIT], "tool_use"),
        _answer([CITED], "end_turn"),
    )
    costs = CostCalculator(DEFAULT_PRICE_TABLE)
    agent = AskAgent(fake.client(), pre_retrieval=False, verify=False, costs=costs)
    events = await _events(agent, QUESTION, _ctx(tmp_path))

    kinds = [type(e).__name__ for e in events]
    assert kinds == [
        "StepStarted", "ToolCalled", "ToolAnswered", "ToolCalled", "ToolAnswered",
        "StepStarted", "ToolCalled", "ToolAnswered",
        "EvidenceChosen", "StepStarted", "TextDelta", "CitationFound", "Done",
    ]  # fmt: skip
    chosen = next(e for e in events if isinstance(e, EvidenceChosen))
    assert (chosen.pieces, chosen.submitted) == (1, 1)  # what was read is within what was sent
    done = events[-1]
    assert isinstance(done, Done)
    answer = done.answer
    assert answer.text == "create_item adds the item to the session."
    assert (answer.stopped, answer.turns, answer.dropped_citations) == ("answered", 3, 1)
    (citation,) = answer.citations
    assert (citation.cited.path, citation.cited.start_line, citation.cited.end_line) == (
        SERVICE, 1, 4,
    )  # fmt: skip
    assert (citation.start, citation.end) == (0, len(answer.text))
    assert answer.cost_usd is not None
    assert answer.cost_usd > 0
    assert len(answer.usage) == 3

    first, second, third = fake.bodies(PATH)
    # The prefix: the tools, then the instructions and the overview, the last one cached.
    assert [t["name"] for t in first["tools"]][:2] == ["code_search", "grep"]
    assert [b.get("cache_control") for b in first["system"]] == [None, {"type": "ephemeral"}]
    assert first["system"][1]["text"] == OVERVIEW
    assert first["cache_control"] == {"type": "ephemeral"}  # the growing tail
    assert first["tool_choice"] == {"type": "auto"}
    assert first["stream"] is True
    assert first["output_config"] == {"effort": "medium"}
    # The assistant's blocks go back as they came (thinking and its signature included), and
    # both results come back in one message.
    assert second["messages"][1]["content"][0] == {
        "type": "thinking", "thinking": "", "signature": "sig",
    }  # fmt: skip
    results = second["messages"][2]["content"]
    assert [(r["type"], r["tool_use_id"]) for r in results] == [
        ("tool_result", "t1"), ("tool_result", "t2"),
    ]  # fmt: skip
    # The answer's turn: the same prefix, no tools to call, the evidence and the question.
    assert (third["tools"], third["system"]) == (first["tools"], first["system"])
    assert third["tool_choice"] == {"type": "none"}
    (asked,) = third["messages"]
    content = asked["content"]
    assert content[0] == _text(INTRO)
    assert _titles(third) == [f"{SERVICE}: saves it"]
    assert content[-1] == _text(QUESTION)


async def test_an_answer_without_evidence_is_reminded_once(tmp_path: Path) -> None:
    fake = _fake(
        _answer([_text("In create_item.")], "end_turn"),
        _answer([_text("Still create_item.")], "end_turn"),
    )
    events = await _events(
        AskAgent(fake.client(), pre_retrieval=False, verify=False), QUESTION, _ctx(tmp_path)
    )
    second = fake.bodies(PATH)[1]
    assert second["messages"][-1] == {"role": "user", "content": EVIDENCE_REMINDER}
    done = events[-1]
    assert isinstance(done, Done)
    # Nothing was read to cite: the research's answer is the answer.
    assert (done.answer.text, done.answer.turns) == ("Still create_item.", 2)


async def test_research_out_of_turns_answers_from_what_it_read(tmp_path: Path) -> None:
    fake = _fake(_answer([READ], "tool_use"), _answer([_text("So.")], "end_turn"))
    agent = AskAgent(fake.client(), pre_retrieval=False, verify=False, budget=Budget(max_turns=2))
    events = await _events(agent, QUESTION, _ctx(tmp_path))
    starts = [e for e in events if isinstance(e, StepStarted)]
    assert [s.tools for s in starts] == [True, False]
    last = fake.bodies(PATH)[-1]
    assert last["tool_choice"] == {"type": "none"}
    assert _titles(last) == [SERVICE]
    assert last["messages"][-1]["content"][-1] == _text(f"{QUESTION}\n\n{LAST_TURN_NOTE}")
    done = events[-1]
    assert isinstance(done, Done)
    assert done.answer.stopped == "turns"


async def test_a_run_past_its_budget_answers_with_what_it_has(tmp_path: Path) -> None:
    fake = _fake(
        _answer([READ], "tool_use", input_tokens=5000),
        _answer([_text("Partly.")], "end_turn"),
    )
    agent = AskAgent(
        fake.client(), pre_retrieval=False, verify=False, budget=Budget(max_input_tokens=4000)
    )
    events = await _events(agent, QUESTION, _ctx(tmp_path))
    last = fake.bodies(PATH)[-1]
    assert last["tool_choice"] == {"type": "none"}
    assert last["messages"][-1]["content"][-1] == _text(f"{QUESTION}\n\n{BUDGET_NOTE}")
    done = events[-1]
    assert isinstance(done, Done)
    assert (done.answer.stopped, done.answer.turns) == ("budget", 2)


class _Reversing(Reranker):
    """Ranks the candidates last first."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def rerank(
        self,
        query: str,
        candidates: Sequence[Candidate],
        mode: RerankMode,
        *,
        reasons: bool | None = None,
        on_rerank_usage: RerankUsageSink | None = None,
        on_llm_usage: LLMUsageSink | None = None,
    ) -> Ranking:
        self.queries.append(f"{mode.value}: {query}")
        return Ranking(tuple(Ranked(c) for c in reversed(candidates)), "llm")


async def test_what_was_read_besides_is_reranked_against_the_question(tmp_path: Path) -> None:
    fake = _fake(
        _answer([_tool("t1", "read_code", {"path": "README.md"}), READ], "tool_use"),
        _answer([_text("Done reading.")], "end_turn"),
        _answer([_text("Still done.")], "end_turn"),  # reminded, it still submits nothing
        _answer([_text("create_item.")], "end_turn"),
    )
    reranker = _Reversing()
    agent = AskAgent(fake.client(), pre_retrieval=False, verify=False, reranker=reranker)
    await _events(agent, QUESTION, _ctx(tmp_path))
    assert reranker.queries == [f"agent: {QUESTION}"]
    assert _titles(fake.bodies(PATH)[-1]) == [SERVICE, "README.md"]  # read the other way round


async def test_the_answer_streams_and_a_refusal_says_so(tmp_path: Path) -> None:
    fake = _fake(_answer([_text("No.")], "refusal"))
    events = await _events(
        AskAgent(fake.client(), pre_retrieval=False, verify=False), "x", _ctx(tmp_path)
    )
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["No."]
    done = events[-1]
    assert isinstance(done, Done)
    assert done.answer.stopped == "refused"
    assert len(fake.bodies(PATH)) == 1  # no answer turn after a refusal


async def test_earlier_exchanges_are_the_conversation(tmp_path: Path) -> None:
    fake = _fake(_answer([_text("Yes.")], "end_turn"), _answer([_text("Yes.")], "end_turn"))
    earlier = (Exchange("where is an item saved?", "In create_item."),)
    agent = AskAgent(fake.client(), pre_retrieval=False, verify=False)
    await _events(agent, "and deleted?", _ctx(tmp_path), conversation=earlier)
    body = fake.bodies(PATH)[0]
    assert body["messages"] == [
        {"role": "user", "content": "where is an item saved?"},
        {"role": "assistant", "content": "In create_item."},
        {"role": "user", "content": "and deleted?"},
    ]


@pytest.mark.parametrize(
    ("question", "mode", "tier", "effort"),
    [
        ("where is create_item defined?", Mode.FAST, Tier.STANDARD, "low"),
        ("how is a new item created?", Mode.FAST, Tier.STANDARD, "medium"),
        ("how is a new item created?", Mode.DEEP, Tier.DEEP, "high"),
        ("what is the architecture of the backend?", Mode.FAST, Tier.DEEP, "high"),
        ("what breaks if I change create_item?", Mode.FAST, Tier.DEEP, "high"),
    ],
)
def test_the_question_picks_the_model_and_effort(
    question: str, mode: Mode, tier: Tier, effort: str
) -> None:
    plan = plan_for(classify(question).route, mode)
    assert (plan.tier, plan.effort) == (tier, effort)


async def test_deep_runs_on_the_deep_model(tmp_path: Path) -> None:
    fake = _fake(_answer([_text("Yes.")], "end_turn"), _answer([_text("Yes.")], "end_turn"))
    events = await _events(
        AskAgent(fake.client(), pre_retrieval=False, verify=False),
        "x",
        _ctx(tmp_path),
        mode=Mode.DEEP,
    )
    body = fake.bodies(PATH)[0]
    assert (body["model"], body["output_config"]) == ("claude-opus-5-5", {"effort": "high"})
    done = events[-1]
    assert isinstance(done, Done)
    assert done.answer.escalated


async def test_prewarm_writes_the_prefix_only(tmp_path: Path) -> None:
    fake = _fake()
    await AskAgent(fake.client(), pre_retrieval=False, verify=False).prewarm(
        _ctx(tmp_path), overview=OVERVIEW
    )
    (body,) = fake.bodies(PATH)
    assert body["max_tokens"] == 0
    assert "stream" not in body
    assert "cache_control" not in body  # the placeholder would key the entry
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"] == {"effort": "medium"}
    assert len(body["tools"]) == 9


# ---------------------------------------------------------------- the final evidence (4.4)


def test_gather_keeps_what_was_submitted_first_and_each_piece_once() -> None:
    evidence = [Evidence("sym:a.py::f", "a.py", 10, 30, "svc", "does it")]
    read = [
        Source("a.py", "s", ((12, 20),)),  # within the submitted piece
        Source("b.py", "s", ((1, 12), (13, 20))),
        Source("b.py", "s", ((5, 9),)),  # within the piece before
        Source("a.py", "s", ((25, 40),)),  # overlaps, isn't within: kept
    ]
    pieces = gather(evidence, read)
    assert [(p.path, p.start_line, p.end_line, p.submitted) for p in pieces] == [
        ("a.py", 10, 30, True), ("b.py", 1, 20, False), ("a.py", 25, 40, False),
    ]  # fmt: skip


async def test_the_final_set_is_capped_and_titled_by_why(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    pieces = [Piece(SERVICE, 1, 4, why="saves it", layer="svc")] + [
        Piece("README.md", 1, 1) for _ in range(MAX_PIECES + 5)
    ]
    final = await final_set(ctx, QUESTION, pieces, None)
    assert len(final) == MAX_PIECES
    assert final[0].submitted
    blocks, sources, shown = answer_blocks(ctx, final[:2])
    assert blocks[0] == _text(INTRO)
    assert [b["title"] for b in blocks if b["type"] == "search_result"] == [
        f"{SERVICE}: saves it", "README.md",
    ]  # fmt: skip
    assert [(s.path, s.start_line, s.end_line) for s in sources] == [
        (SERVICE, 1, 4), ("README.md", 1, 1),
    ]  # fmt: skip
    gone, _, nothing = answer_blocks(ctx, [Piece("deleted.py", 1, 3)])
    assert gone == [_text(INTRO)]  # a piece whose file can't be read is left out
    assert nothing == []
    assert shown == final[:2]


async def test_a_submitted_piece_too_long_for_what_is_left_is_cut_not_dropped(
    tmp_path: Path,
) -> None:
    """80 lines of dense values are more than the answer's evidence may hold: the piece is
    shown, its first lines, rather than left out."""
    ctx = _ctx(tmp_path)
    dense = "".join(
        f"v{n} = '{hashlib.sha256(str(n).encode()).hexdigest() * 6}'\n" for n in range(80)
    )
    (tmp_path / "wide.py").write_text(dense)
    ctx = dataclasses.replace(ctx, tree=sorted([*ctx.tree, "wide.py"]))
    pieces = [
        Piece("wide.py", 1, 80, why="the values", layer="other"),
        Piece(SERVICE, 1, 4, why="saves it", layer="svc"),
    ]
    _, sources, shown = answer_blocks(ctx, pieces)
    assert shown[0].path == "wide.py"
    assert sources[0].path == "wide.py"
    assert 1 < sources[0].end_line < 80
