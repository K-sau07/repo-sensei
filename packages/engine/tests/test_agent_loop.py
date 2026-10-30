"""The Ask agent's loop (4.2) on recorded answers: tool calls run and answered in one message,
the turn cap and the budget end in an answer, citations resolve, the prefix is cached, deep
questions escalate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from sensei_core.usage import DEFAULT_PRICE_TABLE, CostCalculator
from sensei_engine.agent.loop import (
    BUDGET_NOTE,
    LAST_TURN_NOTE,
    AgentEvent,
    AskAgent,
    Budget,
    Done,
    Exchange,
    Mode,
    StepStarted,
    TextDelta,
    ToolAnswered,
    plan_for,
)
from sensei_engine.agent.tools import ToolContext
from sensei_engine.llm.anthropic_client import Tier
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
            "search_result_index": 7,  # nothing the run gave
            "start_block_index": 0,
            "end_block_index": 1,
        },
    ],
}


async def test_a_run_calls_tools_answers_them_together_and_cites(tmp_path: Path) -> None:
    fake = _fake(
        _answer([{"type": "thinking", "thinking": "", "signature": "sig"}, READ, LIST], "tool_use"),
        _answer([CITED], "end_turn"),
    )
    agent = AskAgent(fake.client(), pre_retrieval=False, costs=CostCalculator(DEFAULT_PRICE_TABLE))
    events = await _events(agent, "where is an item saved?", _ctx(tmp_path))

    kinds = [type(e).__name__ for e in events]
    assert kinds == [
        "StepStarted", "ToolCalled", "ToolAnswered", "ToolCalled", "ToolAnswered",
        "StepStarted", "TextDelta", "CitationFound", "Done",
    ]  # fmt: skip
    done = events[-1]
    assert isinstance(done, Done)
    answer = done.answer
    assert answer.text == "create_item adds the item to the session."
    assert answer.stopped == "answered"
    assert answer.turns == 2
    assert answer.dropped_citations == 1
    (citation,) = answer.citations
    assert (citation.cited.path, citation.cited.start_line, citation.cited.end_line) == (
        SERVICE, 1, 4,
    )  # fmt: skip
    assert (citation.start, citation.end) == (0, len(answer.text))
    assert answer.cost_usd is not None
    assert answer.cost_usd > 0
    assert len(answer.usage) == 2

    first, second = fake.bodies(PATH)
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
    assert results[0]["content"][0]["type"] == "search_result"
    called = [e for e in events if isinstance(e, ToolAnswered)]
    assert [c.is_error for c in called] == [False, False]


async def test_the_last_turn_is_an_answer(tmp_path: Path) -> None:
    fake = _fake(
        _answer([READ], "tool_use"), _answer([{"type": "text", "text": "So."}], "end_turn")
    )
    agent = AskAgent(fake.client(), pre_retrieval=False, budget=Budget(max_turns=2))
    events = await _events(agent, "where is an item saved?", _ctx(tmp_path))
    starts = [e for e in events if isinstance(e, StepStarted)]
    assert [s.tools for s in starts] == [True, False]
    last = fake.bodies(PATH)[-1]
    assert last["tool_choice"] == {"type": "none"}
    assert last["messages"][-1]["content"][-1] == {"type": "text", "text": LAST_TURN_NOTE}
    done = events[-1]
    assert isinstance(done, Done)
    assert done.answer.stopped == "turns"


async def test_a_run_past_its_budget_answers_with_what_it_has(tmp_path: Path) -> None:
    fake = _fake(
        _answer([READ], "tool_use", input_tokens=5000),
        _answer([{"type": "text", "text": "Partly."}], "end_turn"),
    )
    agent = AskAgent(fake.client(), pre_retrieval=False, budget=Budget(max_input_tokens=4000))
    events = await _events(agent, "where is an item saved?", _ctx(tmp_path))
    last = fake.bodies(PATH)[-1]
    assert last["tool_choice"] == {"type": "none"}
    assert last["messages"][-1]["content"][-1] == {"type": "text", "text": BUDGET_NOTE}
    done = events[-1]
    assert isinstance(done, Done)
    assert (done.answer.stopped, done.answer.turns) == ("budget", 2)


async def test_the_answer_streams_and_a_refusal_says_so(tmp_path: Path) -> None:
    fake = _fake(_answer([{"type": "text", "text": "No."}], "refusal"))
    events = await _events(AskAgent(fake.client(), pre_retrieval=False), "x", _ctx(tmp_path))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["No."]
    done = events[-1]
    assert isinstance(done, Done)
    assert done.answer.stopped == "refused"


async def test_earlier_exchanges_are_the_conversation(tmp_path: Path) -> None:
    fake = _fake(_answer([{"type": "text", "text": "Yes."}], "end_turn"))
    earlier = (Exchange("where is an item saved?", "In create_item."),)
    await _events(
        AskAgent(fake.client(), pre_retrieval=False),
        "and deleted?",
        _ctx(tmp_path),
        conversation=earlier,
    )
    (body,) = fake.bodies(PATH)
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
    fake = _fake(_answer([{"type": "text", "text": "Yes."}], "end_turn"))
    events = await _events(
        AskAgent(fake.client(), pre_retrieval=False), "x", _ctx(tmp_path), mode=Mode.DEEP
    )
    (body,) = fake.bodies(PATH)
    assert (body["model"], body["output_config"]) == ("claude-opus-5-5", {"effort": "high"})
    done = events[-1]
    assert isinstance(done, Done)
    assert done.answer.escalated


async def test_prewarm_writes_the_prefix_only(tmp_path: Path) -> None:
    fake = _fake()
    await AskAgent(fake.client(), pre_retrieval=False).prewarm(_ctx(tmp_path), overview=OVERVIEW)
    (body,) = fake.bodies(PATH)
    assert body["max_tokens"] == 0
    assert "stream" not in body
    assert "cache_control" not in body  # the placeholder would key the entry
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"] == {"effort": "medium"}
    assert len(body["tools"]) == 9
