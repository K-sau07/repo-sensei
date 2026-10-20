"""Repository URL parsing, the index estimate and repository settings."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from sensei_core.errors import InvalidError
from sensei_core.repos.estimate import estimate_index
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


def test_estimates_grow_with_code_and_cost_money() -> None:
    small = estimate_index(1_000, {"Python": 1_000_000}, CALC)
    large = estimate_index(1_000, {"Python": 50_000_000}, CALC)
    assert small.files < large.files
    assert small.index_minutes < large.index_minutes
    assert Decimal(0) < small.cost_usd < large.cost_usd
    # Heavier-to-parse languages take longer for the same amount of code.
    java = estimate_index(1_000, {"Java": 50_000_000}, CALC)
    assert java.index_minutes > large.index_minutes


def test_language_bytes_beat_the_on_disk_size() -> None:
    """Langflow, as GitHub reported it: 1.6 GB on disk (history, assets), 57 MB of code. The
    estimate follows the code (the repository has several thousand files, not 80 thousand)."""
    langflow = {"Python": 40_929_561, "TypeScript": 12_096_284, "JavaScript": 4_193_846}
    estimate = estimate_index(1_635_492, langflow, CALC)
    assert 5_000 < estimate.files < 10_000
    assert 10_000_000 < estimate.tokens < 25_000_000
    assert estimate.cost_usd < Decimal(5)


def test_without_languages_a_share_of_the_size_is_used() -> None:
    estimate = estimate_index(100_000, {}, CALC)  # 100 MB on disk
    assert 1_000 < estimate.files < 2_000
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
