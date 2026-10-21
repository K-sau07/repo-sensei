"""The index job state machine: which moves are allowed, progress and ETA.

Status::

    queued ─► running ─► succeeded
       │         ├────► failed
       └─────────┴────► cancelled

Stages only move forward (``clone`` … ``finalize``); a stage that isn't needed (an incremental
run) is passed over and recorded as ``skipped``. A stage can be completed on its own (one task
does one stage; the next task advances), and a job succeeds only from ``finalize``.

Progress (0..1) weighs stages by their usual share of the work. The ETA for the running stage
comes from a moving average of its rate (items per second); later stages add their expected
durations when known (``expected_seconds``), otherwise the ETA covers the running stage only.
Every change is also recorded in ``events``, for the caller to publish.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sensei_core.indexing.features import Feature, unlocked
from sensei_core.models import IndexJob, JobStatus, Stage

STAGES: tuple[Stage, ...] = tuple(Stage)
# Typical share of an index run's time (sums to 100).
STAGE_WEIGHTS: dict[Stage, int] = {
    Stage.CLONE: 5,
    Stage.DISCOVER: 3,
    Stage.MERKLE: 2,
    Stage.PARSE: 20,
    Stage.SYMBOLS: 10,
    Stage.CHUNKS: 10,
    Stage.EMBEDDINGS: 30,
    Stage.SUMMARIES: 10,
    Stage.GRAPH: 5,
    Stage.LINKS: 3,
    Stage.FINALIZE: 2,
}
EMA_ALPHA = 0.3  # weight of the newest rate sample
LOG_LEVELS = frozenset({"info", "warn", "error"})

_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class IllegalTransitionError(Exception):
    """The job can't make that move from where it is."""


@dataclass(frozen=True, slots=True)
class JobEvent:
    type: str  # job.started, stage.started, stage.progress, stage.finished, job.succeeded, ...
    stage: Stage | None = None
    data: dict[str, Any] = field(default_factory=dict[str, Any])


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _time(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


class IndexJobStateMachine:
    def __init__(
        self,
        job: IndexJob,
        *,
        now: Callable[[], datetime] = _utcnow,
        expected_seconds: Mapping[Stage, float] | None = None,
    ) -> None:
        self.job = job
        self._now = now
        self._expected = dict(expected_seconds or {})
        self.events: list[JobEvent] = []

    # ------------------------------------------------------------------ moves

    def start(self, first: Stage = Stage.CLONE) -> None:
        self._move_to(JobStatus.RUNNING)
        self.job.started_at = self._now()
        self._emit("job.started")
        stages = self._stages()
        for stage in STAGES[: STAGES.index(first)]:
            stages[stage.value] = {"status": "skipped"}
        self._enter(stages, first)
        self._save(stages)

    def advance(self, stage: Stage) -> None:
        """Finish the running stage and start ``stage`` (a later one; stages in between are
        skipped)."""
        current = self._require_running_stage()
        if STAGES.index(stage) <= STAGES.index(current):
            msg = f"can't go from {current.value} back or sideways to {stage.value}"
            raise IllegalTransitionError(msg)
        stages = self._stages()
        if stages[current.value].get("status") == "running":
            self._complete(stages, current)
        for between in STAGES[STAGES.index(current) + 1 : STAGES.index(stage)]:
            stages[between.value] = {"status": "skipped"}
        self._enter(stages, stage)
        self._save(stages)

    def complete(self, stage: Stage) -> None:
        """Mark the running ``stage`` done without starting the next one."""
        self._require_current_running(stage)
        stages = self._stages()
        self._complete(stages, stage)
        self._save(stages)

    def note(self, stage: Stage, **details: Any) -> None:
        """Attach facts to a stage (limitations, counts) for the UI and for debugging."""
        current = self._require_running_stage()
        if stage is not current:
            msg = f"{stage.value} isn't the current stage (the job is at {current.value})"
            raise IllegalTransitionError(msg)
        stages = self._stages()
        stages[stage.value].setdefault("details", {}).update(details)
        self._save(stages)

    def update_progress(self, stage: Stage, done: int, total: int) -> None:
        self._require_current_running(stage)
        if total < 0 or not 0 <= done <= total:
            msg = f"progress must satisfy 0 <= done <= total (got {done}/{total})"
            raise ValueError(msg)
        stages = self._stages()
        entry = stages[stage.value]
        previous_done = int(entry.get("done") or 0)
        if done < previous_done:
            msg = f"progress can't go backwards ({previous_done} -> {done})"
            raise ValueError(msg)
        now = self._now()
        since = _time(entry.get("updated_at")) or _time(entry.get("started_at")) or now
        elapsed = (now - since).total_seconds()
        if elapsed > 0 and done > previous_done:
            sample = (done - previous_done) / elapsed
            rate = entry.get("rate")
            entry["rate"] = (
                sample if rate is None else EMA_ALPHA * sample + (1 - EMA_ALPHA) * float(rate)
            )
        entry.update(done=done, total=total)
        if done > previous_done or entry.get("updated_at") is None:
            # Only on progress: a stall then counts in the next sample (time since the last
            # step forward), instead of disappearing into repeated reports of the same count.
            entry["updated_at"] = now.isoformat()
        self._save(stages)
        self._emit("stage.progress", stage, done=done, total=total)

    def keep_alive(self, stage: Stage, until: datetime) -> None:
        """Record that ``stage`` is alive until ``until`` without progress: a retry waiting out
        a rate limit or a backoff. The stall sweep counts from the later of this and the last
        progress. No event: nothing the user sees changes."""
        self._require_current_running(stage)
        stages = self._stages()
        entry = stages[stage.value]
        known = _time(entry.get("alive_until"))
        if known is not None and known >= until:
            return
        entry["alive_until"] = until.isoformat()
        self._save(stages)

    def finish(self) -> None:
        current = self._require_running_stage()
        if current is not Stage.FINALIZE:
            msg = f"a job finishes from {Stage.FINALIZE.value}, not {current.value}"
            raise IllegalTransitionError(msg)
        stages = self._stages()
        if stages[current.value].get("status") == "running":
            self._complete(stages, current)
        self._move_to(JobStatus.SUCCEEDED)
        self.job.finished_at = self._now()
        self._save(stages)
        self.job.progress, self.job.eta_s = 1.0, 0
        self._emit("job.succeeded")

    def fail(self, stage: Stage, error: Mapping[str, Any]) -> None:
        """``error``: at least a ``code`` and a ``message`` safe to show."""
        self._require_current_running(stage)
        stages = self._stages()
        details = {"stage": stage.value, **dict(error)}
        stages[stage.value].update(
            status="failed", finished_at=self._now().isoformat(), error=details
        )
        self._move_to(JobStatus.FAILED)
        self.job.error = details
        self.job.finished_at = self._now()
        self._save(stages)
        self.job.eta_s = None
        self._emit("job.failed", stage, error=details)

    def fail_waiting(self, error: Mapping[str, Any]) -> None:
        """Fail a running job between two stages (its stage is done, the next never started).
        The finished stage stays done; the error is the job's."""
        current = self._require_running_stage()
        status = (self.job.stages or {}).get(current.value, {}).get("status")
        if status == "running":
            msg = f"{current.value} is running: use fail()"
            raise IllegalTransitionError(msg)
        details = {"stage": current.value, **dict(error)}
        self._move_to(JobStatus.FAILED)
        self.job.error = details
        self.job.finished_at = self._now()
        self.job.eta_s = None
        self._emit("job.failed", current, error=details)

    def cancel(self) -> None:
        current = self.job.stage
        self._move_to(JobStatus.CANCELLED)
        stages = self._stages()
        if current is not None and stages.get(current.value, {}).get("status") == "running":
            stages[current.value].update(status="cancelled", finished_at=self._now().isoformat())
        self.job.finished_at = self._now()
        self._save(stages)
        self.job.eta_s = None
        self._emit("job.cancelled", current)

    def log(self, message: str, *, level: str = "info", stage: Stage | None = None) -> None:
        """A line for the job's log as the user sees it (no state change; nothing secret)."""
        if level not in LOG_LEVELS:
            msg = f"log level must be one of {sorted(LOG_LEVELS)}"
            raise ValueError(msg)
        self._emit("job.log", stage or self.job.stage, level=level, message=message)

    # ------------------------------------------------------------------ reading

    def completed_stages(self) -> list[Stage]:
        stages = self.job.stages or {}
        return [s for s in STAGES if stages.get(s.value, {}).get("status") == "done"]

    def features(self) -> list[Feature]:
        return unlocked(self.completed_stages())

    # ------------------------------------------------------------------ internals

    def _move_to(self, status: JobStatus) -> None:
        if status not in _TRANSITIONS[self.job.status]:
            msg = f"a {self.job.status.value} job can't become {status.value}"
            raise IllegalTransitionError(msg)
        self.job.status = status

    def _require_current_running(self, stage: Stage) -> None:
        current = self._require_running_stage()
        status = (self.job.stages or {}).get(current.value, {}).get("status")
        if stage is not current or status != "running":
            msg = f"{stage.value} isn't running (the job is at {current.value}, {status})"
            raise IllegalTransitionError(msg)

    def _require_running_stage(self) -> Stage:
        if self.job.status is not JobStatus.RUNNING or self.job.stage is None:
            msg = f"the job isn't running (it is {self.job.status.value})"
            raise IllegalTransitionError(msg)
        return self.job.stage

    def _stages(self) -> dict[str, Any]:
        # A copy: assigning a new dict is what tells SQLAlchemy the JSON column changed.
        return copy.deepcopy(self.job.stages or {})

    def _enter(self, stages: dict[str, Any], stage: Stage) -> None:
        stages[stage.value] = {
            "status": "running",
            "done": 0,
            "total": None,
            "rate": None,
            "started_at": self._now().isoformat(),
            "updated_at": None,
            "finished_at": None,
            "error": None,
        }
        self.job.stage = stage
        self._emit("stage.started", stage)

    def _complete(self, stages: dict[str, Any], stage: Stage) -> None:
        entry = stages[stage.value]
        if entry.get("total") is not None:
            entry["done"] = entry["total"]
        entry.update(status="done", finished_at=self._now().isoformat())
        self._emit("stage.finished", stage, unlocks=[f.value for f in unlocked([stage])])

    def _save(self, stages: dict[str, Any]) -> None:
        self.job.stages = stages
        self.job.progress = self._progress(stages)
        self.job.eta_s = self._eta(stages)

    def _progress(self, stages: dict[str, Any]) -> float:
        earned = 0.0
        for stage in STAGES:
            entry = stages.get(stage.value, {})
            if entry.get("status") in ("done", "skipped"):
                earned += STAGE_WEIGHTS[stage]
            elif entry.get("status") == "running" and entry.get("total"):
                earned += STAGE_WEIGHTS[stage] * int(entry["done"]) / int(entry["total"])
        return round(min(1.0, earned / sum(STAGE_WEIGHTS.values())), 4)

    def _eta(self, stages: dict[str, Any]) -> int | None:
        current = self.job.stage
        if current is None:
            return None
        entry = stages.get(current.value, {})
        rate, total = entry.get("rate"), entry.get("total")
        if not rate or total is None:
            return None  # no rate yet: unknown rather than a made-up number
        remaining = (int(total) - int(entry.get("done") or 0)) / float(rate)
        later = STAGES[STAGES.index(current) + 1 :]
        remaining += sum(self._expected.get(stage, 0.0) for stage in later)
        return math.ceil(remaining)

    def _emit(self, kind: str, stage: Stage | None = None, **data: Any) -> None:
        self.events.append(JobEvent(type=kind, stage=stage, data=data))
