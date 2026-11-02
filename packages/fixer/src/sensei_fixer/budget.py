"""What one fix may spend (8.4): its agents' turns, model cost, and time in the sandbox. A fix
over any of them stops (``failed``, saying which) at the next check, between steps."""

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

    def over(self, run: FixRun) -> str | None:
        """Why ``run`` is over its budget (None: it isn't)."""
        if run.spent_turns > self.max_turns:
            return f"it took more than {self.max_turns} agent turns"
        if run.spent_cost_usd > self.max_cost_usd:
            return f"it cost more than ${self.max_cost_usd}"
        if run.spent_sandbox_seconds > self.max_sandbox_seconds:
            return f"it ran in the sandbox for more than {self.max_sandbox_seconds / 60:g} minutes"
        return None
