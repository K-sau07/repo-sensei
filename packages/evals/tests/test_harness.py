"""The harness validated with its stub retrievers: the oracle scores 1.0 everywhere, and the
random retriever lands on the baseline that chance predicts (file recall@k = k / N)."""

from __future__ import annotations

import random
import tarfile
from pathlib import Path

import pytest

from sensei_evals.retrieval import Hit, OracleRetriever, RandomRetriever, RepoRef, file_corpus
from sensei_evals.runner import aggregate, run_suite
from sensei_evals.schema import GoldenItem, GoldTarget, Layer, QueryClass, Suite

SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e"
REPO = RepoRef(id="demo", sha=SHA)
FIXTURE = (
    Path(__file__).parents[3]
    / "packages"
    / "engine"
    / "tests"
    / "fixtures"
    / "fastapi-template.tar.gz"
)


def _item(n: int, gold: list[GoldTarget], cls: QueryClass = QueryClass.CONCEPTUAL) -> GoldenItem:
    answerable = bool(gold)
    return GoldenItem(
        id=f"item-{n}",
        repo="demo",
        sha=SHA,
        query=f"question number {n}",
        class_=cls if answerable else QueryClass.UNANSWERABLE,
        layers=sorted({t.layer for t in gold}),
        gold=gold,
        answerable=answerable,
    )


def _mixed_suite() -> Suite:
    flow = [
        GoldTarget(layer=Layer.UI, path="web/Login.tsx", symbol="Login", lines=(10, 20)),
        GoldTarget(layer=Layer.HANDLER, path="api/login.py", symbol="login", lines=(5, 9)),
        GoldTarget(layer=Layer.DB, path="api/models.py"),
    ]
    items = [
        _item(1, flow, QueryClass.FLOW),
        _item(2, [GoldTarget(layer=Layer.CONFIG, path="compose.yml")], QueryClass.REGEX),
        _item(3, []),
    ]
    return Suite(name="mixed", version=0, items=items)


async def test_oracle_scores_one_on_every_metric() -> None:
    suite = _mixed_suite()
    run = await run_suite(suite, suite.items, OracleRetriever(suite.items), REPO, ks=[10, 20])
    overall = run.metrics["overall"]
    # Every retrieval metric is defined for the two answerable items and is perfect...
    assert {name for name, value in overall.items() if value != 1.0} == set()
    # ...and the unanswerable item gets nothing back, which counts as abstaining.
    unanswerable = run.per_item[2]
    assert unanswerable.abstained
    assert set(unanswerable.metrics.values()) == {None}
    assert run.metrics["counts"] == {
        "items": 3,
        "answerable": 2,
        "by_class": {"flow": 1, "regex": 1, "unanswerable": 1},
    }
    assert all(not item.missed for item in run.per_item)
    assert (run.config["ks"], run.config["depth"], run.repo_sha) == ([10, 20], 20, SHA)


def _random_world(files: int, items: int, gold_per_item: int) -> tuple[list[Hit], Suite]:
    corpus = [Hit(path=f"src/f{n:04d}.py", lines=(1, 50)) for n in range(files)]
    rng = random.Random(7)  # noqa: S311 - test data
    suite_items = [
        _item(
            n,
            [
                GoldTarget(layer=Layer.SERVICE, path=hit.path)
                for hit in rng.sample(corpus, gold_per_item)
            ],
        )
        for n in range(items)
    ]
    return corpus, Suite(name="random-world", version=0, items=suite_items)


@pytest.mark.parametrize("k", [10, 20, 50, 100])
async def test_random_retriever_hits_the_chance_baseline(k: int) -> None:
    files, items = 200, 400
    corpus, suite = _random_world(files, items, gold_per_item=2)
    run = await run_suite(suite, suite.items, RandomRetriever(corpus, seed=1), REPO, ks=[k])
    expected = k / files
    # Per-item recall is 0, 1/2 or 1; over 400 items the standard error is under 0.02.
    assert run.metrics["overall"][f"recall_file@{k}"] == pytest.approx(expected, abs=0.04)
    # Random retrieval returns hits for every question, so it never abstains: all answerable.
    assert run.metrics["overall"]["abstention_accuracy"] == 1.0


async def test_random_retriever_is_repeatable() -> None:
    corpus, _ = _random_world(50, 5, gold_per_item=1)
    first = await RandomRetriever(corpus, seed=3).search("q", REPO, 10)
    again = await RandomRetriever(corpus, seed=3).search("q", REPO, 10)
    other = await RandomRetriever(corpus, seed=4).search("q", REPO, 10)
    assert first == again != other
    assert len({hit.path for hit in first}) == 10
    assert len(await RandomRetriever(corpus[:3]).search("q", REPO, 10)) == 3


def test_file_corpus_covers_the_text_files_of_a_checkout(tmp_path: Path) -> None:
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(tmp_path, filter="data")
    corpus = file_corpus(tmp_path / "fastapi-template")
    paths = {hit.path for hit in corpus}
    assert "backend/app/api/routes/login.py" in paths
    assert not any(path.endswith(".png") for path in paths)  # binary: no lines to cover
    login = next(hit for hit in corpus if hit.path == "backend/app/api/routes/login.py")
    assert login.lines == (1, 123)  # wc -l at the pinned commit


async def test_oracle_cut_below_the_gold_count_loses_recall() -> None:
    suite = _mixed_suite()
    run = await run_suite(suite, suite.items[:1], OracleRetriever(suite.items), REPO, ks=[1, 3])
    # The flow item has 3 gold targets in 3 files and layers: one hit finds one of each.
    assert run.per_item[0].metrics["recall_file@1"] == pytest.approx(1 / 3)
    assert run.per_item[0].metrics["all_layers@1"] == 0.0
    assert run.per_item[0].metrics["recall_symbol@3"] == 1.0


def test_aggregate_of_nothing() -> None:
    assert aggregate([])["overall"] == {"abstention_accuracy": None}
