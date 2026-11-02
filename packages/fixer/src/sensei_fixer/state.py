"""The fix state machine (8.4).

Stages only move forward::

    intake ─► investigate ─► reproduce ─► root_cause ─► plan ─► awaiting_approval
                                                                      │ (a person approves)
    done ◄─ pr ◄─ verify ◄─ patch ◄───────────────────────────────────┘

Status says what that means: ``queued`` (its next stage is waiting for a worker), ``running``
(a stage is), ``awaiting_approval``, ``succeeded`` (done: a draft pull request), or a stop:
``not_reproduced``, ``blocked`` (a rule), ``failed``, ``rejected`` (a person said no),
``cancelled``. A run waiting for triage (``needs_triage``: a Sentry issue under the gate)
starts only when someone starts it. Every change is recorded in ``events`` for the caller to
publish once it has committed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from sensei_core.models import FixRun, FixStage, FixStatus
from sensei_core.models.fixes import TERMINAL

STAGES: tuple[FixStage, ...] = tuple(FixStage)
NEXT: dict[FixStage, FixStage] = dict(pairwise(STAGES))
STOPS = frozenset(TERMINAL - {FixStatus.SUCCEEDED})


class FixStateError(Exception):
    """A move the state machine doesn't allow (what was asked, and the run's state)."""


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class FixStateMachine:
    run: FixRun
    clock: Callable[[], datetime] = utcnow
    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def _event(self, kind: str, **extra: Any) -> None:
        self.events.append(
            {
                "type": kind,
                "fix_id": str(self.run.id),
                "stage": self.run.stage.value,
                "status": self.run.status.value,
                "ts": self.clock().isoformat(),
                **extra,
            }
        )

    def _refuse(self, what: str) -> FixStateError:
        return FixStateError(
            f"can't {what}: the fix is {self.run.status.value} at {self.run.stage.value}"
        )

    def start(self) -> None:
        """Out of triage (or a rerun's first queue): queued at its stage."""
        if self.run.status is not FixStatus.NEEDS_TRIAGE:
            raise self._refuse("start it")
        self.run.status = FixStatus.QUEUED
        self._event("queued")

    def begin_stage(self) -> FixStage:
        """A worker takes the stage it is queued at."""
        if self.run.status is not FixStatus.QUEUED:
            raise self._refuse("begin a stage")
        self.run.status = FixStatus.RUNNING
        self._event("stage_started")
        return self.run.stage

    def advance(self) -> FixStage:
        """The running stage finished: on to the next (queued for a worker, or waiting for
        approval, or done)."""
        if self.run.status is not FixStatus.RUNNING:
            raise self._refuse("finish a stage")
        finished = self.run.stage
        self.run.stage = NEXT[finished]
        if self.run.stage is FixStage.AWAITING_APPROVAL:
            self.run.status = FixStatus.AWAITING_APPROVAL
        elif self.run.stage is FixStage.DONE:
            self.run.status = FixStatus.SUCCEEDED
            self.run.finished_at = self.clock()
        else:
            self.run.status = FixStatus.QUEUED
        self._event("stage_done", finished=finished.value)
        if self.run.status is FixStatus.AWAITING_APPROVAL:
            self._event("awaiting_approval")
        elif self.run.status is FixStatus.SUCCEEDED:
            self._event("done", pr_url=self.run.pr_url)
        return self.run.stage

    def approve(self) -> None:
        """A person approved the plan: on to the patch."""
        if self.run.status is not FixStatus.AWAITING_APPROVAL:
            raise self._refuse("approve it")
        self.run.stage = FixStage.PATCH
        self.run.status = FixStatus.QUEUED
        self._event("approved")

    def replan(self) -> None:
        """A person edited the plan: planned again, then back to approval."""
        if self.run.status is not FixStatus.AWAITING_APPROVAL:
            raise self._refuse("plan it again")
        self.run.stage = FixStage.PLAN
        self.run.status = FixStatus.QUEUED
        self._event("replanning")

    def retry(self) -> None:
        """A run that couldn't be reproduced, tried again (with more context in its ticket):
        from the investigation, its earlier findings and test dropped."""
        if self.run.status is not FixStatus.NOT_REPRODUCED:
            raise self._refuse("retry it")
        self.run.stage = FixStage.INVESTIGATE
        self.run.status = FixStatus.QUEUED
        self.run.stop_reason = None
        self.run.finished_at = None
        self.run.root_cause = None
        self.run.repro = None
        self.run.repro_result = None
        self._event("retrying")

    def stop(self, status: FixStatus, reason: str) -> None:
        """An end short of a pull request (any time before one, except twice)."""
        if status not in STOPS:
            raise FixStateError(f"{status.value} isn't a way to stop")
        if self.run.status in TERMINAL:
            raise self._refuse(f"stop it as {status.value}")
        self.run.status = status
        self.run.stop_reason = reason[:2000]
        self.run.finished_at = self.clock()
        self._event(status.value, reason=self.run.stop_reason)
