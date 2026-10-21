"""A rough size, time and cost estimate for indexing a repository, before connecting it.

Heuristic. GitHub's per-language byte counts measure the code in the default branch, so they are
the basis when present. GitHub's ``size`` counts the whole repository on disk (history, assets),
which can be many times larger (Langflow: 1.6 GB on disk, 57 MB of code), so it is only a fallback
and only a share of it counts. Calibrated against real index runs later; until then the numbers are
labelled estimates in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sensei_core.usage import CostCalculator

SOURCE_SHARE = 0.1  # of the on-disk size, when GitHub reports no languages
BYTES_PER_FILE = 8_000
CHARS_PER_TOKEN = 3.5
BASE_MINUTES = 0.5
FILES_PER_MINUTE = 2_500
# Relative parse cost per language (bytes-weighted); others count as 1.
LANGUAGE_COST = {
    "TypeScript": 1.2,
    "JavaScript": 1.1,
    "Java": 1.3,
    "Kotlin": 1.3,
    "C++": 1.4,
    "C#": 1.2,
    "Go": 0.9,
    "Rust": 1.2,
    "Python": 1.0,
    "Markdown": 0.4,
}


@dataclass(frozen=True, slots=True)
class IndexEstimate:
    files: int
    tokens: int
    index_minutes: float
    cost_usd: Decimal


def estimate_index(size_kb: int, languages: dict[str, int], calc: CostCalculator) -> IndexEstimate:
    total = sum(size for size in languages.values() if size > 0)
    source_bytes = total if total else max(0, size_kb) * 1024 * SOURCE_SHARE
    files = max(1, round(source_bytes / BYTES_PER_FILE))
    tokens = round(source_bytes / CHARS_PER_TOKEN)
    factor = (
        sum(LANGUAGE_COST.get(lang, 1.0) * size for lang, size in languages.items()) / total
        if total
        else 1.0
    )
    minutes = round(BASE_MINUTES + files / FILES_PER_MINUTE * factor, 1)
    embed = calc.cost("voyage", "voyage-code-4", tokens_in=tokens) or Decimal(0)
    compute = calc.cost("sensei", "index-worker", units=Decimal(str(minutes))) or Decimal(0)
    return IndexEstimate(
        files=files, tokens=tokens, index_minutes=minutes, cost_usd=embed + compute
    )
