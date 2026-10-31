"""Answer evals (4.14), offline: the judge's verdicts as a score, an answer's timing and
scores, the release gate, and a run that stops at its budget. The paid run is ``sensei-eval
answers``; nothing here calls a model."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from textwrap import dedent
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from sensei_core.usage import DEFAULT_PRICE_TABLE, CostCalculator
from sensei_engine.agent.confidence import Abstention
from sensei_engine.agent.loop import (
    Answer,
    AnswerCitation,
    Done,
    Mode,
    StepStarted,
    TextDelta,
)
from sensei_engine.agent.tools.base import Cited, ToolContext
from sensei_engine.agent.verify import Checked, Claim, Verification
from sensei_engine.llm.anthropic_client import LLMError, LLMRequest, LLMResult, Tier
from sensei_evals import answers
from sensei_evals.answers import (
    AnswerOutcome,
    Judge,
    ask,
    gate,
    judge_prompt,
    read_verdicts,
    run_answers,
    score_answer,
)
from sensei_evals.cli import app
from sensei_evals.reports import render_run
from sensei_evals.rerank import CostGuard
from sensei_evals.retrieval import RepoRef
from sensei_evals.schema import parse_suite

SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e"
SUITE = parse_suite(
    dedent(
        f"""\
        name: answers-demo
        version: 0
        items:
          - id: login
            repo: fastapi-template
            sha: {SHA}
            query: how is a password checked at sign-in?
            class: conceptual
            layers: [service]
            gold:
              - {{layer: service, path: backend/app/crud.py, symbol: authenticate, lines: [45, 60]}}
              - {{layer: service, path: backend/app/core/security.py, lines: [29, 32]}}
            answer:
              text: "`crud.authenticate` looks the user up and checks the hash."
              checklist:
                - {{fact: "`authenticate` verifies the password against its hash", weight: 4}}
                - {{fact: "a missing user still costs a hash check", weight: 2}}
          - id: billing
            repo: fastapi-template
            sha: {SHA}
            query: how are invoices emailed?
            class: unanswerable
            answerable: false
            answer:
              text: The repository has no billing; it sends no invoices.
              checklist:
                - {{fact: says the repository has no billing, weight: 4}}
        """
    )
)
LOGIN, BILLING = SUITE.items


def _cited(path: str, start: int, end: int) -> Cited:
    return Cited(path=path, sha=SHA, start_line=start, end_line=end, symbol_key=None)


def _answer(
    citations: list[Cited], *, supported: int, abstained: bool = False, cost: str = "0.04"
) -> Answer:
    claims = tuple(
        Checked(
            Claim(n=n, text="a claim", start=0, end=1, cited=(c,)),
            "supported" if n <= supported else "unsupported",
        )
        for n, c in enumerate(citations, start=1)
    )
    return Answer(
        text="It checks the hash.",
        citations=tuple(AnswerCitation(0, 1, c, "") for c in citations),
        stopped="answered",
        model="claude-sonnet-5-5",
        turns=2,
        usage=(),
        cost_usd=Decimal(cost),
        evidence=(),
        escalated=False,
        verification=Verification(
            claims=claims,
            dropped_citations=0,
            flagged=(),
            unknown_identifiers=(),
            whole=None,
            rounds=0,
        ),
        abstained=Abstention((), None, "thin", "ask about X") if abstained else None,
    )


# ---------------------------------------------------------------- the judge


def test_verdicts_score_the_checklist_by_weight() -> None:
    judged = read_verdicts(
        LOGIN,
        {"facts": [{"n": 1, "verdict": "stated", "why": "says so"},
                   {"n": 2, "verdict": "partly", "why": "vague"}]},
    )  # fmt: skip
    assert judged.score == pytest.approx((4 * 1 + 2 * 0.5) / 6)
    assert judged.contradicted == 0
    # A fact left out is missing; one the answer contradicts earns nothing and is counted.
    judged = read_verdicts(LOGIN, {"facts": [{"n": 1, "verdict": "contradicted", "why": "no"}]})
    assert (judged.score, judged.contradicted) == (0.0, 1)
    assert judged.verdicts[1] == ("missing", "not graded")
    assert read_verdicts(LOGIN, "not json").score == 0.0


def test_the_judge_reads_the_reference_and_numbers_the_facts() -> None:
    prompt = judge_prompt(LOGIN, "It checks the hash.")
    assert "1. `authenticate` verifies the password against its hash" in prompt
    assert "<reference_answer>\n`crud.authenticate` looks the user up" in prompt
    assert prompt.endswith("<answer>\nIt checks the hash.\n</answer>")
    assert "(no answer)" in judge_prompt(LOGIN, "")


class _Client:
    def __init__(self) -> None:
        self.asked: list[LLMRequest] = []

    def model_for(self, tier: Tier) -> str:
        return f"model-{tier.value}"

    async def complete(self, request: LLMRequest, *, on_usage: Any = None) -> LLMResult:
        self.asked.append(request)
        data = {"facts": [{"n": 1, "verdict": "stated", "why": "yes"}]}
        return LLMResult(text="", data=data, model="model-deep", usage=())


async def test_the_judge_grades_on_the_deep_tier_with_a_schema() -> None:
    client = _Client()
    judge = Judge(cast("Any", client))
    judged = await judge.judge(BILLING, "There is no billing code.")
    assert judged.score == 1.0
    (request,) = client.asked
    assert request.tier is Tier.DEEP
    assert request.schema is answers.JUDGE_SCHEMA
    assert judge.model == "model-deep"


# ---------------------------------------------------------------- one question


class _Agent:
    def __init__(self, events: list[Any], fail: Exception | None = None) -> None:
        self.events = events
        self.fail = fail
        self.asked: list[str] = []

    async def run(
        self, question: str, ctx: ToolContext, *, mode: Mode = Mode.FAST
    ) -> AsyncIterator[Any]:
        self.asked.append(question)
        for event in self.events:
            yield event
        if self.fail is not None:
            raise self.fail


CTX = cast("ToolContext", object())


async def test_a_question_is_timed_to_its_first_event_and_first_token() -> None:
    answer = _answer([_cited("backend/app/crud.py", 45, 60)], supported=1)
    agent = _Agent(
        [
            StepStarted(1, "m", "medium", tools=True),
            TextDelta(1, "Let me look."),  # a research turn's: reasoning, not the answer
            StepStarted(2, "m", "medium", tools=False),
            TextDelta(2, ""),
            TextDelta(2, "It"),
            Done(answer),
        ]
    )
    outcome = await ask(agent, "q", CTX, mode=Mode.FAST)
    assert outcome.answer is answer
    assert outcome.first_event_ms is not None
    assert outcome.first_token_ms is not None
    assert outcome.first_event_ms <= outcome.first_token_ms <= outcome.total_ms
    reasoned = await ask(
        _Agent([StepStarted(1, "m", None, tools=True), TextDelta(1, "x")]), "q", CTX,
        mode=Mode.FAST,
    )  # fmt: skip
    assert reasoned.first_token_ms is None  # it never wrote an answer
    failed = await ask(_Agent(["step"], LLMError(500, "down")), "q", CTX, mode=Mode.FAST)
    # A run that failed gave no answer and didn't abstain.
    assert (failed.answer, failed.abstained, failed.error) == (None, False, "Anthropic 500: down")


class _NoJudge:
    model = "none"

    async def judge(self, item: object, text: str) -> object:
        raise AssertionError("a failed run has nothing to judge")


async def test_a_failed_run_is_a_failure_not_an_abstention() -> None:
    guard = CostGuard(Decimal("1"), CostCalculator(DEFAULT_PRICE_TABLE))
    agent = _Agent([], LLMError(500, "down"))
    record = await run_answers(
        SUITE, SUITE.items, agent=agent, contexts=_contexts, judge=cast("Judge", _NoJudge()),
        guard=guard, repo=REPO,
    )  # fmt: skip
    assert record.metrics["overall"]["failed"] == 1.0  # both failed: the rate, not a flag
    assert record.metrics["overall"]["abstention_accuracy"] is None  # none answered to judge
    assert record.metrics["overall"]["checklist"] == 0.0  # not judged, and nothing stated
    assert [item.error for item in record.per_item] == ["Anthropic 500: down"] * 2


def test_an_answer_is_scored_on_its_citations_and_checklist() -> None:
    answer = _answer(
        [_cited("backend/app/crud.py", 45, 50), _cited("README.md", 1, 3)], supported=1
    )
    outcome = AnswerOutcome(answer, 120.0, 900.0, 4000.0)
    judged = read_verdicts(LOGIN, {"facts": [{"n": 1, "verdict": "stated", "why": ""}]})
    scores = score_answer(LOGIN, outcome, judged)
    assert scores["citation_recall"] == 0.5  # crud.authenticate, not verify_password
    assert scores["citation_in_gold"] == 0.5  # README.md is in no gold file
    assert scores["citation_supported"] == 0.5  # the checks backed one of the two
    assert scores["checklist"] == pytest.approx(4 / 6)
    assert scores["cost_usd"] == pytest.approx(0.04)
    assert (scores["first_event_ms"], scores["first_token_ms"]) == (120.0, 900.0)
    file_f = scores["f0.5_file"]
    assert file_f is not None
    assert 0 < file_f < 1
    # An unanswerable item has no gold to find: only what applies to it.
    none = score_answer(BILLING, AnswerOutcome(None, None, None, 10.0), None)
    assert "f0.5_file" not in none
    assert none["checklist"] == 0.0  # the run failed: it stated none of the checklist


# ---------------------------------------------------------------- the gate


def test_the_gate_names_what_it_misses_and_counts_the_unmeasured_as_missed() -> None:
    metrics = {
        "overall": {"citation_supported": 0.97, "abstention_accuracy": 0.9},
        "answer": {
            "first_event_ms": {"p50": 300.0},
            "first_token_ms": {"lookup": {"p50": 4000.0}, "trace": {"p50": None}},
            "cost_usd": {"mean": 0.21},
        },
    }
    assert [(m.metric, m.bound) for m in gate(metrics)] == [
        ("cost_usd.mean", "< 0.15"),
        ("first_token_ms.trace.p50", "< 15000"),
    ]


# ---------------------------------------------------------------- the run


@asynccontextmanager
async def _contexts() -> AsyncGenerator[ToolContext]:
    yield CTX


REPO = RepoRef(id="fastapi-template", sha=SHA, checkout=Path("/nowhere"))


async def test_a_run_asks_every_item_and_records_the_gate() -> None:
    answer = _answer([_cited("backend/app/crud.py", 45, 60)], supported=1)
    agent = _Agent([TextDelta(1, "It"), Done(answer)])
    guard = CostGuard(Decimal("1"), CostCalculator(DEFAULT_PRICE_TABLE))
    record = await run_answers(
        SUITE, SUITE.items, agent=agent, contexts=_contexts, judge=None, guard=guard, repo=REPO
    )
    assert agent.asked == [LOGIN.query, BILLING.query]
    assert [r.id for r in record.per_item] == ["login", "billing"]
    assert record.retriever == "ask:fast"
    assert record.config["stopped"] is None
    # Answered both: right for login, wrong for billing (it should have abstained).
    assert record.metrics["overall"]["abstention_accuracy"] == 0.5
    assert record.metrics["overall"]["failed"] == 0.0
    assert {m["metric"] for m in record.metrics["gate"]} >= {"abstention_accuracy"}
    shown = render_run(record)  # the report renders an answer run as it does a retrieval one
    assert "citation_supported" in shown


async def test_a_run_stops_at_its_budget_with_what_it_did() -> None:
    guard = CostGuard(Decimal("0.01"), CostCalculator(DEFAULT_PRICE_TABLE))
    guard.spent = Decimal("0.01")
    record = await run_answers(
        SUITE, SUITE.items, agent=_Agent([]), contexts=_contexts, judge=None, guard=guard,
        repo=REPO,
    )  # fmt: skip
    assert record.per_item == []
    assert "budget" in str(record.config["stopped"])


# ---------------------------------------------------------------- the command


@pytest.mark.parametrize(("budget", "said"), [("0", "more than 0"), ("lots", "number of dollars")])
def test_the_command_wants_a_budget_before_anything_is_paid(budget: str, said: str) -> None:
    result = CliRunner().invoke(app, ["answers", "--suite", "x", "--budget", budget])
    assert result.exit_code != 0
    assert said in result.output
