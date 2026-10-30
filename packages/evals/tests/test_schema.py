"""Golden suite validation, and errors that point at the right line."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from sensei_evals.schema import Layer, QueryClass, SuiteError, load_suite, parse_suite

SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e"

VALID = dedent(
    f"""\
    name: demo
    version: 0
    items:
      - id: demo-flow-01
        repo: demo
        sha: {SHA}
        query: where does the sign-in form send the password
        class: flow
        layers: [ui, handler]
        gold:
          - {{layer: ui, path: web/Login.tsx, symbol: Login, lines: [10, 20]}}
          - layer: handler
            path: api/login.py
      - id: demo-none-01
        repo: demo
        sha: {SHA}
        query: how are invoices emailed
        class: unanswerable
        answerable: false
        notes: there is no billing code
    """
)


def _problems(text: str) -> list[tuple[int | None, str, str]]:
    with pytest.raises(SuiteError) as caught:
        parse_suite(text, "suite.yaml")
    return [(p.line, p.where, p.message) for p in caught.value.problems]


def test_valid_suite_loads(tmp_path: Path) -> None:
    path = tmp_path / "demo.yaml"
    path.write_text(VALID, encoding="utf-8")
    suite = load_suite(path)
    assert (suite.name, suite.version, suite.repos) == ("demo", 0, ["demo"])
    flow, none = suite.items
    assert flow.class_ is QueryClass.FLOW
    assert flow.layers == [Layer.UI, Layer.HANDLER]
    assert flow.gold[0].lines == (10, 20)
    assert flow.gold[1].lines is None
    assert (none.answerable, none.gold, none.notes) == (False, [], "there is no billing code")
    assert suite.for_repo("other") == []


def test_errors_name_the_line_and_field() -> None:
    text = (
        VALID.replace("class: flow", "class: flw")
        .replace("lines: [10, 20]", "lines: [20, 10]")
        .replace("path: api/login.py", "path: ../login.py\n        line: 3")
    )
    assert _problems(text) == [
        (8, "items.0.class", _problems(text)[0][2]),
        (11, "items.0.gold.0", "lines must be [start, end] with 1 <= start <= end"),
        (13, "items.0.gold.1.path", _problems(text)[2][2]),
        (14, "items.0.gold.1.line", "Extra inputs are not permitted"),
    ]
    assert "'flow'" in _problems(text)[0][2]
    assert "relative path" in _problems(text)[2][2]


def test_rendered_error_reads_like_a_compiler_error() -> None:
    with pytest.raises(SuiteError) as caught:
        parse_suite(VALID.replace(f"sha: {SHA}", "sha: main", 1), "golden/demo.yaml")
    assert str(caught.value) == (
        "golden/demo.yaml:6: items.0.sha: must be a full 40-character lowercase commit sha"
    )


def test_yaml_syntax_error_has_a_line() -> None:
    assert _problems("name: demo\nitems: [\n") == [
        (3, "", _problems("name: demo\nitems: [\n")[0][2])
    ]


def test_duplicate_keys_are_rejected() -> None:
    [(line, _, message)] = _problems(VALID.replace("repo: demo\n", "repo: demo\n    repo: x\n", 1))
    assert (line, message) == (6, "duplicate key 'repo'")


def test_numbers_are_not_strings() -> None:
    [(line, where, _)] = _problems(VALID.replace("id: demo-flow-01", "id: 123"))
    assert (line, where) == (4, "items.0.id")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("class: flow", "class: unanswerable"), "an unanswerable item must set answerable: false"),
        (
            ("layers: [ui, handler]", "layers: [ui, handler, db]"),
            "layers without a gold target: db",
        ),
        (("layers: [ui, handler]", "layers: [ui]"), "gold targets in layers the item doesn't list"),
        (("layers: [ui, handler]", "layers: [ui, ui, handler]"), "layers must not repeat"),
        ((", lines: [10, 20]", ""), "a target with a symbol must give the symbol's lines"),
    ],
)
def test_item_consistency(change: tuple[str, str], message: str) -> None:
    problems = _problems(VALID.replace(*change, 1))
    assert any(message in text for _, _, text in problems), problems


def test_unanswerable_items_have_no_gold() -> None:
    text = VALID.replace(
        "notes: there is no billing code",
        "gold:\n      - {layer: ui, path: web/Login.tsx}",
    )
    [(line, where, message)] = _problems(text)
    assert (line, where, message) == (14, "items.1", "an unanswerable item has no gold targets")


def test_answerable_items_need_gold() -> None:
    text = VALID.replace("class: unanswerable\n    answerable: false\n", "class: conceptual\n")
    assert any("needs at least one gold target" in m for _, _, m in _problems(text))


def test_item_ids_are_unique() -> None:
    text = VALID.replace("id: demo-none-01", "id: demo-flow-01")
    [(line, where, message)] = _problems(text)
    assert (line, where, message) == (1, "", "duplicate item id 'demo-flow-01'")


def test_false_premise_items_keep_their_class() -> None:
    text = VALID.replace("class: unanswerable", "class: conceptual")
    assert parse_suite(text).items[1].class_ is QueryClass.CONCEPTUAL


def test_queries_are_unique_per_repo() -> None:
    text = VALID.replace(
        "how are invoices emailed", "where does the sign-in form send the password"
    )
    [(line, where, message)] = _problems(text)
    assert (line, where) == (1, "")
    assert message == "item 'demo-none-01' repeats an earlier query for demo"


def test_an_item_may_carry_a_reference_answer_and_its_checklist() -> None:
    answered = VALID.replace(
        "      - layer: handler\n        path: api/login.py\n",
        "      - layer: handler\n        path: api/login.py\n"
        "    answer:\n"
        "      text: The form posts to /login, whose handler checks the hash.\n"
        "      checklist:\n"
        "        - {fact: it posts to /login, weight: 4}\n"
        "        - fact: the handler checks the hash\n",
    )
    assert answered != VALID
    flow, none = parse_suite(answered, "suite.yaml").items
    assert flow.answer is not None
    assert [(f.fact, f.weight) for f in flow.answer.checklist] == [
        ("it posts to /login", 4),
        ("the handler checks the hash", 1),  # a detail, unless it says otherwise
    ]
    assert none.answer is None
    bad = answered.replace("weight: 4", "weight: 5")
    assert [(line, where) for line, where, _ in _problems(bad)] == [
        (17, "items.0.answer.checklist.0.weight")
    ]
