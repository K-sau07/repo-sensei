"""What one fix may spend (8.4): its agents' turns, model cost, and time in the sandbox. A fix
that has used any of them up stops (``failed``, saying which) at the next check, between steps,
before another turn; one whose stage went past it is stopped when the stage finishes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sensei_core.config import FixerSettings
from sensei_core.models import FixRun


@dataclass(frozen=True, slots=True)
class Budget:
    max_turns: int = 120
    max_cost_usd: Decimal = Decimal("3.00")
    max_sandbox_seconds: float = 1800.0

    @classmethod
    def from_settings(cls, settings: FixerSettings) -> Budget:
        return cls(
            max_turns=settings.max_turns,
            max_cost_usd=settings.max_cost_usd,
            max_sandbox_seconds=settings.max_sandbox_minutes * 60,
        )

    def exhausted(self, run: FixRun) -> str | None:
        """Why ``run`` can't take another turn (it has used its budget up; None: it can)."""
        if run.spent_turns >= self.max_turns:
            return f"it used its {self.max_turns} agent turns"
        if run.spent_cost_usd >= self.max_cost_usd:
            return f"it used its ${self.max_cost_usd} of model cost"
        if run.spent_sandbox_seconds >= self.max_sandbox_seconds:
            return f"it used its {self.max_sandbox_seconds / 60:g} minutes in the sandbox"
        return None

    def over(self, run: FixRun) -> str | None:
        """Why ``run`` went past its budget (None: it didn't; using all of it is fine)."""
        if run.spent_turns > self.max_turns:
            return f"it took more than {self.max_turns} agent turns"
        if run.spent_cost_usd > self.max_cost_usd:
            return f"it cost more than ${self.max_cost_usd}"
        if run.spent_sandbox_seconds > self.max_sandbox_seconds:
            return f"it ran in the sandbox for more than {self.max_sandbox_seconds / 60:g} minutes"
        return None
