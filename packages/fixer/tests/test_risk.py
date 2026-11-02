"""Risk labels and merge readiness (8.7): high for auth, payments, migrations and concurrency
(by path, by the graph, by what the change says and does), for a bug that wasn't reproduced and
for a large diff; medium and low otherwise; readiness 0-5 with its reasons."""

from __future__ import annotations

from typing import Any

import pytest

from sensei_core.models import FixRisk
from sensei_fixer.risk import (
    Areas,
    assess,
    code_areas,
    path_areas,
    readiness,
    text_areas,
    words,
)


def _areas(paths: list[str] = [], texts: list[str] = [], code: list[str] = []) -> Areas:  # noqa: B006
    areas = Areas()
    path_areas(paths, areas)
    text_areas([("the plan", t) for t in texts], areas)
    code_areas([("app/x.py", line) for line in code], areas)
    return areas


@pytest.mark.parametrize(
    ("areas", "area"),
    [
        (_areas(["api/auth/login.py"]), "auth"),
        (_areas(["web/src/lib/billing/invoices.ts"]), "payments"),
        (_areas(["backend/alembic/versions/0012_add_plan.py"]), "migrations"),
        (_areas(["db/migrations/20260101_users.sql"]), "migrations"),
        (_areas(texts=["Two requests race on the counter's update"]), "concurrency"),
        (_areas(code=["    with self._lock:  # threading.Lock"]), "concurrency"),
        (_areas(code=["    row = q.with_for_update().first()"]), "concurrency"),
        (_areas(["app/services/passwordReset.py"]), "auth"),
    ],
)
def test_sensitive_areas_are_found(areas: Areas, area: str) -> None:
    assert area in areas.found


def test_ordinary_code_touches_no_area() -> None:
    plain = _areas(["app/db/session.py", "app/schemas.py", "app/stats.py",
                    "web/src/components/Tokenizer.tsx"],
                   ["Return 0 for an empty list instead of dividing by zero"])  # fmt: skip
    assert plain.found == {}
    assert words("getUserByID_v2") == {"get", "user", "by", "id", "2"}  # single letters dropped


def test_risk_labels() -> None:
    def level(**kwargs: object) -> FixRisk:
        found: dict[str, Any] = {"areas": Areas(), "reproduced": True, "files": 1, "lines": 6,
                                 "blast": None, **kwargs}  # fmt: skip
        return assess(**found).level

    assert level() is FixRisk.LOW
    assert level(areas=_areas(["app/payments/refund.py"])) is FixRisk.HIGH
    assert level(reproduced=False) is FixRisk.HIGH
    assert level(lines=201) is FixRisk.HIGH
    assert level(files=6) is FixRisk.HIGH
    assert level(lines=80) is FixRisk.MEDIUM
    assert level(files=3) is FixRisk.MEDIUM
    assert level(blast={"counts": {"routes": 4}}) is FixRisk.MEDIUM
    assert level(blast={"counts": {"tables": 1}}) is FixRisk.MEDIUM
    risk = assess(areas=_areas(["api/auth/login.py"]), reproduced=False, files=1, lines=6,
                  blast=None)  # fmt: skip
    assert risk.reasons == ("touches auth: api/auth/login.py is auth code",
                            "the bug wasn't reproduced")  # fmt: skip


def test_merge_readiness() -> None:
    best = readiness(risk=FixRisk.LOW, known_failures=0, concerns=(), covered=True,
                     extra_failed=False, extra_written=True)  # fmt: skip
    assert best.score == 5
    assert best.reason.startswith("Reproduced;")
    middling = readiness(risk=FixRisk.HIGH, known_failures=2, concerns=("A caller relies on it",),
                         covered=False, extra_failed=False, extra_written=True)  # fmt: skip
    assert middling.score == 0
    assert "High risk" in middling.reason
    assert "2 test(s) failed before" in middling.reason
    failing = readiness(risk=FixRisk.LOW, known_failures=0, concerns=(), covered=True,
                        extra_failed=True, extra_written=True)  # fmt: skip
    assert failing.score == 1
    assert "extra test" in failing.reason
