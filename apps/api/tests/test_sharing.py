"""A shared conversation as a viewer who can't read its repository sees it (4.11): the prose
and how sure it is, nothing of the code, each citation still where it was in the text."""

from __future__ import annotations

from typing import Any

import pytest

from sensei_api.sharing import HIDDEN, shown

TEXT = (
    "Logins are checked by `verify_token`.\n"
    "```python\nSTRIPE_KEY = os.environ['STRIPE_KEY']\n```\n"
    "Then the session is saved."
)
FIRST = TEXT.index("`.") + 2
LAST = TEXT.index("Then")
INSIDE = TEXT.index("STRIPE")


def _parts() -> list[dict[str, Any]]:
    def cite(n: int, span: list[int]) -> dict[str, Any]:
        data = {"n": n, "span": span, "path": "app/auth.py", "lines": [3, 9], "sha": "a" * 40}
        return {"type": "data-citation", "id": f"citation-{n}", "data": data}

    abstain = {
        "reason": "Nothing it read refunds a payment.",
        "searched": ["grep 'refund'"],
        "trailEnd": {"end": "sym:app/billing.py::charge", "reason": "no caller"},
        "suggestion": "Ask about sym:app/billing.py::refund directly",
    }
    return [
        {"type": "data-conversation", "id": "conversation", "data": {"id": "c", "runId": "r"}},
        {"type": "step-start"},
        {"type": "tool-read_code", "toolCallId": "t1", "input": {"path": "app/auth.py"}},
        {"type": "reasoning", "text": "app/auth.py has it"},
        {"type": "text", "text": TEXT},
        cite(1, [0, FIRST]),
        cite(2, [LAST, len(TEXT)]),
        cite(3, [INSIDE, INSIDE + 6]),
        {"type": "data-confidence", "id": "confidence", "data": {"level": "high"}},
        {"type": "data-searchlog", "id": "searchlog", "data": {"queries": ["grep 'auth'"]}},
        {"type": "data-abstain", "id": "abstain", "data": abstain},
    ]


def test_a_viewer_who_cant_read_the_repository_sees_no_code() -> None:
    out = shown(_parts(), redact=True)
    assert [p["type"] for p in out] == [
        "text",
        "data-citation",
        "data-citation",
        "data-citation",
        "data-confidence",
        "data-abstain",
    ]
    text = out[0]["text"]
    assert HIDDEN in text
    assert "STRIPE" not in str(out)
    assert "app/" not in str(out)  # no path: not a citation's, the trail's or the suggestion's
    spans = {p["data"]["n"]: p["data"]["span"] for p in out if p["type"] == "data-citation"}
    assert text[slice(*spans[1])] == "Logins are checked by `verify_token`."
    assert text[slice(*spans[2])] == "Then the session is saved."
    assert spans[3][0] == spans[3][1] == text.index(HIDDEN) + len(HIDDEN)  # after the note
    assert all(set(p["data"]) == {"n", "span", "redacted"} for p in out[1:4])
    assert out[-1]["data"] == {
        "reason": "Nothing it read refunds a payment.",
        "searched": [],
        "trailEnd": None,
        "suggestion": "",
    }


def test_a_reader_sees_it_whole_but_for_the_run_behind_it() -> None:
    parts = _parts()
    out = shown(parts, redact=False)
    assert out == [p for p in parts if p["type"] != "data-conversation"]


@pytest.mark.parametrize(
    "answer",
    [
        "Steps:\n1. Load it:\n   ```python\n   SECRET = load()\n   ```\n2. Done.",
        "It reads:\n\n    SECRET = load()\n    use(SECRET)\n\nThen it returns.",
        "Tabbed:\n\n\tSECRET = load()\n",
        "Unclosed:\n  ~~~\n  SECRET = load()",
        # Indented code that follows no blank line, as markdown renders it all the same.
        "## Code\n    SECRET = load()",
        "```\nfirst()\n```\n    SECRET = load()",
        "Above.\n\n---\n    SECRET = load()",
        "x:\n   \n    SECRET = load()",
        "Raw:\n\n<pre>\nSECRET = load()\n</pre>",
    ],
)
def test_code_in_a_list_item_or_indented_is_hidden_too(answer: str) -> None:
    out = shown([{"type": "text", "text": answer}], redact=True)
    text = out[0]["text"]
    assert "SECRET" not in text
    assert HIDDEN in text


def test_prose_around_hidden_code_stays() -> None:
    answer = "First the route.\n\n    SECRET = load()\n\nThen `save` writes it."
    text = shown([{"type": "text", "text": answer}], redact=True)[0]["text"]
    assert text.startswith("First the route.\n\n")
    assert text.endswith("Then `save` writes it.")


def test_citations_are_placed_in_the_last_text_wherever_it_sits() -> None:
    """An answer searched again (or rewritten) comes again whole after the first, and after its
    citations: their spans are offsets into it, the code hidden in the first doesn't move them."""
    draft = "A first go.\n```\nSECRET = 1\n```\nMore of it."
    final = "It saves the item. Then it commits."
    parts: list[dict[str, Any]] = [
        {"type": "text", "text": draft},
        {"type": "data-citation", "id": "c1", "data": {"n": 1, "span": [0, 18]}},
        {"type": "text", "text": final},
    ]
    out = shown(parts, redact=True)
    (citation,) = [p for p in out if p["type"] == "data-citation"]
    assert final[slice(*citation["data"]["span"])] == "It saves the item."
