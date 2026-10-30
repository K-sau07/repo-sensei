"""The Ask agent's events as the UI Message Stream (protocol v1): a golden sequence, the
message the client assembles from it, and a run that fails."""

from __future__ import annotations

from decimal import Decimal

from sensei_api.ui_stream import UIStream, text_of
from sensei_engine.agent.confidence import confidence
from sensei_engine.agent.loop import (
    Answer,
    AnswerCitation,
    CitationFound,
    Done,
    EvidenceChosen,
    StepStarted,
    TextDelta,
    ToolAnswered,
    ToolCalled,
    Verified,
)
from sensei_engine.agent.tools.base import Cited
from sensei_engine.agent.verify import Checked, Claim, Verification, Whole

SHA = "b" * 40
CITED = Cited("backend/app/crud.py", SHA, 3, 9, "py backend/app/crud.py/create_item().")
CITATION = AnswerCitation(0, 25, CITED, "3\tdef create_item")
TEXT = "create_item saves the item."
VERIFIED = Verification(
    (Checked(Claim(1, TEXT, 0, len(TEXT), (CITED,)), "supported"),),
    0,
    (),
    (),
    Whole(True, (), ""),
    0,
)


def _answer() -> Answer:
    return Answer(
        TEXT,
        (CITATION,),
        "answered",
        "claude-sonnet-5-5",
        2,
        (),
        Decimal("0.01"),
        (),
        False,
        0,
        VERIFIED,
        confidence(VERIFIED, None),
        None,
        ("round 0 'where is an item saved'", "read_code 'backend/app/crud.py'"),
    )


EVENTS = [
    StepStarted(1, "claude-sonnet-5-5", "medium", tools=True),
    TextDelta(1, "Reading crud."),
    ToolCalled("t1", "read_code", {"path": "backend/app/crud.py"}),
    ToolAnswered("t1", "read_code", "backend/app/crud.py:1-40", 1, False, 4),
    EvidenceChosen(1, 1),
    StepStarted(2, "claude-sonnet-5-5", "medium", tools=False),
    TextDelta(2, "create_item saves "),
    TextDelta(2, "the item."),
    Verified(VERIFIED),
    CitationFound(1, CITATION),
    Done(_answer()),
]


def test_a_run_streams_as_the_protocol_has_it() -> None:
    stream = UIStream("m1", "o/items")
    parts = stream.start() + [part for event in EVENTS for part in stream.encode(event)]
    types = [part["type"] for part in parts]
    assert types == [
        "start",
        "start-step",
        "reasoning-start", "reasoning-delta",  # a research turn's text isn't the answer
        "reasoning-end", "tool-input-start", "tool-input-available", "tool-output-available",
        "data-progress",
        "finish-step", "start-step",
        "text-start", "text-delta", "text-delta", "text-end",
        "data-progress",
        "data-citation",
        "data-confidence", "data-searchlog",
        "finish-step", "finish",
    ]  # fmt: skip
    assert parts[0] == {"type": "start", "messageId": "m1"}
    assert parts[6] == {
        "type": "tool-input-available",
        "toolCallId": "t1",
        "toolName": "read_code",
        "input": {"path": "backend/app/crud.py"},
    }
    deltas = [p for p in parts if p["type"] == "text-delta"]
    assert {p["id"] for p in deltas} == {"text-2"}
    citation = next(p for p in parts if p["type"] == "data-citation")
    assert citation == {
        "type": "data-citation",
        "id": "citation-1",
        "data": {
            "n": 1,
            "repo": "o/items",
            "sha": SHA,
            "path": "backend/app/crud.py",
            "lines": [3, 9],
            "symbolKey": "py backend/app/crud.py/create_item().",
            "span": [0, 25],
            "provenance": "evidence",
            "verified": True,
        },
    }
    sure = next(p for p in parts if p["type"] == "data-confidence")["data"]
    assert sure["level"] == "high"
    assert sure["breakdown"]["claims_verified"] == 1
    log = next(p for p in parts if p["type"] == "data-searchlog")["data"]
    assert log == {"queries": list(_answer().searched)}


def test_the_stream_assembles_the_message_the_client_keeps() -> None:
    stream = UIStream("m1", "o/items")
    for event in EVENTS:
        stream.encode(event)
    kinds = [part["type"] for part in stream.parts]
    assert kinds == [
        "step-start", "reasoning", "tool-read_code", "data-progress", "step-start", "text",
        "data-progress", "data-citation", "data-confidence", "data-searchlog",
    ]  # fmt: skip
    call = stream.parts[2]
    assert (call["state"], call["output"]["summary"]) == (
        "output-available", "backend/app/crud.py:1-40",
    )  # fmt: skip
    assert text_of(stream.parts) == TEXT


def test_a_run_that_fails_says_so_and_ends() -> None:
    stream = UIStream("m1", "o/items")
    stream.start()
    stream.encode(EVENTS[0])
    stream.encode(EVENTS[1])
    parts = stream.failed("The model isn't reachable right now.")
    assert [p["type"] for p in parts] == ["reasoning-end", "error", "finish-step", "finish"]
    assert parts[1] == {"type": "error", "errorText": "The model isn't reachable right now."}
