"""Repository URL parsing, the index estimate and repository settings."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from sensei_core.config import Settings
from sensei_core.errors import InvalidError
from sensei_core.repos.estimate import EstimateModels, TreeStats, estimate_index, tree_stats
from sensei_core.repos.service import RepoSettings
from sensei_core.repos.urls import parse_github_url
from sensei_core.usage import DEFAULT_PRICE_TABLE, CostCalculator


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/langflow-ai/langflow", ("langflow-ai", "langflow")),
        ("https://github.com/langflow-ai/langflow/", ("langflow-ai", "langflow")),
        ("https://github.com/langflow-ai/langflow.git", ("langflow-ai", "langflow")),
        ("https://github.com/langflow-ai/langflow/tree/main/src", ("langflow-ai", "langflow")),
        ("http://www.github.com/a/b?tab=readme#top", ("a", "b")),
        ("github.com/a/b.c_d-e", ("a", "b.c_d-e")),
        ("  https://GitHub.com/Owner/Repo  ", ("Owner", "Repo")),
        ("git@github.com:owner/repo.git", ("owner", "repo")),
    ],
)
def test_github_urls(url: str, expected: tuple[str, str]) -> None:
    assert parse_github_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/a/b",
        "https://github.com.evil.example/a/b",
        "https://evil.example/github.com/a/b",
        "https://user:pass@github.com/a/b",
        "ftp://github.com/a/b",
        "https://github.com/only-owner",
        "https://github.com/",
        "https://github.com/-bad/repo",
        "https://github.com/a/..",
        "https://github.com/a/na me",
        "git@gitlab.com:a/b.git",
        "",
        "https://[github.com/a/b",  # urlsplit: invalid IPv6 URL
        "https://github.com\uff03@evil.com/a/b",  # NFKC normalisation into a separator
        "https://github.com:99999999/a/b",  # port out of range
    ],
)
def test_other_urls_are_refused(url: str) -> None:
    with pytest.raises(InvalidError) as exc:
        parse_github_url(url)
    assert exc.value.code == "invalid_repo_url"


def test_enterprise_hosts_follow_the_configured_web_url() -> None:
    assert parse_github_url("https://git.corp.example/a/b", web_url="https://git.corp.example") == (
        "a",
        "b",
    )
    with pytest.raises(InvalidError):
        parse_github_url("https://github.com/a/b", web_url="https://git.corp.example")


CALC = CostCalculator(DEFAULT_PRICE_TABLE)


# Four repositories as GitHub described them (2026-10-24: size, language bytes, the default
# branch's tree) and the files discovery kept when they were indexed.
MEASURED = {
    "fastapi": (
        57_058, {"Python": 4_626_334, "JavaScript": 1_066},
        TreeStats(3_233, 1_223, 4_649_992, 35_437_078, truncated=False), 3_232,
    ),
    "full-stack-fastapi-template": (
        7_103, {"TypeScript": 317_948, "Python": 90_980, "HTML": 21_286},
        TreeStats(252, 152, 409_084, 1_886_624, truncated=False), 245,
    ),
    "langflow": (
        1_672_161, {"Python": 41_349_533, "TypeScript": 12_250_366, "JavaScript": 4_193_857},
        TreeStats(11_850, 8_415, 58_648_403, 239_895_098, truncated=False), 11_407,
    ),
    "flask-api": (
        1_772, {"Python": 74_007, "HTML": 8_809},
        TreeStats(89, 31, 207_698, 539_236, truncated=False), 87,
    ),
}  # fmt: skip


def test_the_tree_counts_files_and_code() -> None:
    body = {
        "tree": [
            {"path": "src", "type": "tree"},
            {"path": "src/app.py", "type": "blob", "size": 3_000},
            {"path": "src/View.TSX", "type": "blob", "size": 7_000},
            {"path": "README.md", "type": "blob", "size": 500},
            {"path": "vendor/lib", "type": "commit"},  # a submodule
            {"path": "odd.py", "type": "blob", "size": "big"},
            "not an entry",
        ],
        "truncated": True,
    }
    assert tree_stats(body) == TreeStats(4, 3, 10_000, 10_500, truncated=True)
    assert tree_stats({"message": "Not Found"}) is None


@pytest.mark.parametrize("name", list(MEASURED))
def test_with_the_tree_files_are_within_a_few_percent(name: str) -> None:
    size_kb, languages, tree, discovered = MEASURED[name]
    estimate = estimate_index(size_kb, languages, CALC, tree)
    assert abs(estimate.files - discovered) / discovered < 0.05
    assert estimate.tokens == round(tree.code_bytes / 3.5)


@pytest.mark.parametrize("name", list(MEASURED))
def test_without_the_tree_language_bytes_are_within_three_times(name: str) -> None:
    """Language bytes count code only, so docs-heavy repositories come out low and code-heavy
    ones high; the old guess was 5-8 times low on all four."""
    size_kb, languages, _, discovered = MEASURED[name]
    estimate = estimate_index(size_kb, languages, CALC)
    assert discovered / 3 < estimate.files < discovered * 3


def test_a_truncated_tree_is_a_floor() -> None:
    size_kb, languages, _, _ = MEASURED["langflow"]
    cut = TreeStats(1_000, 500, 1_000_000, 5_000_000, truncated=True)
    estimate = estimate_index(size_kb, languages, CALC, cut)
    assert estimate.files == estimate_index(size_kb, languages, CALC).files  # the languages win


def test_time_and_cost_follow_the_measured_stages() -> None:
    size_kb, languages, tree, _ = MEASURED["langflow"]
    langflow = estimate_index(size_kb, languages, CALC, tree)
    # ~59K chunks to embed (~$2.30) and 8,415 file summaries (~$2.20), each priced as its own
    # short request (one 22M-token prompt would be at the long-context rate).
    assert Decimal(4) < langflow.cost_usd < Decimal(6)
    assert 15 < langflow.index_minutes < 30
    size_kb, languages, tree, _ = MEASURED["full-stack-fastapi-template"]
    template = estimate_index(size_kb, languages, CALC, tree)
    assert Decimal("0.05") < template.cost_usd < Decimal("0.15")
    assert template.index_minutes < langflow.index_minutes


def test_embedding_time_respects_the_accounts_tokens_a_minute() -> None:
    size_kb, languages, tree, _ = MEASURED["langflow"]
    paid = estimate_index(size_kb, languages, CALC, tree)
    # ~59K chunks of 330 tokens at 3M a minute: ~6.5 minutes of sending, not ~2.5 at full speed.
    slower = estimate_index(
        size_kb, languages, CALC, tree, EstimateModels(embed_tokens_per_minute=1_500_000)
    )
    assert 6 < slower.index_minutes - paid.index_minutes < 7.5


def test_the_deployments_models_are_priced_and_one_without_a_price_is_named() -> None:
    settings = Settings.model_validate({"anthropic": {"fast_model": "claude-sonnet-5-5"}})
    models = EstimateModels.from_settings(settings)
    # Tests have no keys: neither stage would run.
    assert models == EstimateModels(
        summary_model="claude-sonnet-5-5", embeddings=False, summaries=False
    )
    models = EstimateModels(summary_model="claude-sonnet-5-5")
    size_kb, languages, tree, _ = MEASURED["full-stack-fastapi-template"]
    haiku = estimate_index(size_kb, languages, CALC, tree)
    assert estimate_index(size_kb, languages, CALC, tree, models).cost_usd > haiku.cost_usd
    with capture_logs() as logs:
        unpriced = estimate_index(
            size_kb, languages, CALC, tree, EstimateModels(summary_model="claude-unknown-1")
        )
    assert unpriced.cost_usd < haiku.cost_usd
    assert unpriced.unpriced == ("claude-unknown-1",)
    assert haiku.unpriced == ()
    assert {"event": "estimate leaves out a model without a price", "model": "claude-unknown-1",
            "log_level": "warning"}.items() <= logs[0].items()  # fmt: skip


def test_only_the_stages_that_run_are_priced() -> None:
    size_kb, languages, tree, _ = MEASURED["full-stack-fastapi-template"]
    unknown = EstimateModels(summary_model="claude-unknown-1", summaries=False)
    off = estimate_index(size_kb, languages, CALC, tree, unknown)
    assert off.unpriced == ()  # summaries don't run: their model isn't needed
    full = estimate_index(size_kb, languages, CALC, tree)
    bare = estimate_index(size_kb, languages, CALC, tree, EstimateModels(embeddings=False))
    assert bare.cost_usd < full.cost_usd
    assert bare.index_minutes < full.index_minutes
    # No code files: nothing to summarize, no summary model needed.
    docs = TreeStats(files=12, code_files=0, code_bytes=0, bytes=40_000, truncated=False)
    only_docs = estimate_index(40, {}, CALC, docs, EstimateModels(summary_model="claude-unknown-1"))
    assert only_docs.unpriced == ()


def test_with_nothing_known_a_share_of_the_size_is_used() -> None:
    estimate = estimate_index(100_000, {}, CALC)  # 100 MB on disk
    assert 1_000 < estimate.files < 5_000
    empty = estimate_index(0, {}, CALC)
    assert (empty.files, empty.tokens) == (1, 0)


def test_settings_defaults_and_cleanup() -> None:
    assert RepoSettings().model_dump() == {
        "tracked_branches": [],
        "ignore_globs": [],
        "auto_index": True,
    }
    cleaned = RepoSettings(
        tracked_branches=[" main ", "release/1.x", "main"], ignore_globs=["dist/**", " ", "dist/**"]
    )
    assert cleaned.tracked_branches == ["main", "release/1.x"]
    assert cleaned.ignore_globs == ["dist/**"]


@pytest.mark.parametrize(
    "changes",
    [
        {"tracked_branches": ["has space"]},
        {"tracked_branches": ["bad~name"]},
        {"tracked_branches": [""]},
        *(
            {"tracked_branches": [name]}
            for name in ("-delete", "a..b", "x@{1}", "@", "main.lock", "a/.hidden", "a//b", "a/")
        ),
        {"tracked_branches": [f"b{i}" for i in range(21)]},
        {"ignore_globs": ["x" * 201]},
    ],
)
def test_invalid_settings_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RepoSettings.model_validate(changes)
