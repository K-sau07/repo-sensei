"""Index job state machine: allowed moves, progress, ETA, events and unlocked features."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from sensei_core.indexing.features import UNLOCKS, Feature, unlocked
from sensei_core.indexing.state import (
    STAGE_WEIGHTS,
    STAGES,
    IllegalTransitionError,
    IndexJobStateMachine,
)
from sensei_core.models import IndexJob, JobKind, JobStatus, Stage


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _job(kind: JobKind = JobKind.FULL) -> IndexJob:
    return IndexJob(
        org_id=uuid.uuid4(), repo_id=uuid.uuid4(), kind=kind, status=JobStatus.QUEUED, stages={}
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _machine(clock: Clock, **kwargs: object) -> IndexJobStateMachine:
    return IndexJobStateMachine(_job(), now=clock, **kwargs)  # type: ignore[arg-type]


def _run_to(machine: IndexJobStateMachine, stage: Stage) -> None:
    for next_stage in STAGES[STAGES.index(machine.job.stage or Stage.CLONE) + 1 :]:
        machine.advance(next_stage)
        if next_stage is stage:
            return


# ------------------------------------------------------------------ the happy path


def test_a_full_run_from_queued_to_succeeded(clock: Clock) -> None:
    machine = _machine(clock)
    machine.start()
    assert (machine.job.status, machine.job.stage) == (JobStatus.RUNNING, Stage.CLONE)
    assert machine.job.started_at == clock.now
    progress = [machine.job.progress]
    for stage in STAGES[1:]:
        clock.tick(1)
        machine.advance(stage)
        progress.append(machine.job.progress)
    clock.tick(1)
    machine.finish()
    assert machine.job.status is JobStatus.SUCCEEDED
    assert (machine.job.progress, machine.job.eta_s) == (1.0, 0)
    assert machine.job.finished_at == clock.now
    assert progress == sorted(progress)  # never goes backwards
    assert all(machine.job.stages[s.value]["status"] == "done" for s in STAGES)
    assert machine.features() == list(Feature)

    kinds = [event.type for event in machine.events]
    assert kinds[0] == "job.started"
    assert kinds[-1] == "job.succeeded"
    assert kinds.count("stage.started") == kinds.count("stage.finished") == len(STAGES)
    finished = {e.stage: e.data["unlocks"] for e in machine.events if e.type == "stage.finished"}
    assert finished[Stage.EMBEDDINGS] == ["semantic_search", "ask"]


def test_progress_follows_stage_weights(clock: Clock) -> None:
    machine = _machine(clock)
    machine.start()
    _run_to(machine, Stage.PARSE)  # clone 5 + discover 3 + merkle 2 done
    assert machine.job.progress == 0.1
    machine.update_progress(Stage.PARSE, 500, 1000)  # half of parse's 20
    assert machine.job.progress == 0.2


# ------------------------------------------------------------------ skipping


def test_an_incremental_run_skips_what_it_doesnt_need(clock: Clock) -> None:
    machine = IndexJobStateMachine(_job(JobKind.INCREMENTAL), now=clock)
    machine.start(Stage.MERKLE)
    assert [machine.job.stages[s]["status"] for s in ("clone", "discover")] == [
        "skipped",
        "skipped",
    ]
    machine.advance(Stage.CHUNKS)  # parse and symbols not needed this time
    assert machine.job.stages["symbols"]["status"] == "skipped"
    # Skipped stages unlock nothing: only stages that ran count.
    assert machine.completed_stages() == [Stage.MERKLE]
    assert machine.features() == []


# ------------------------------------------------------------------ illegal moves


def test_moves_that_are_refused(clock: Clock) -> None:
    machine = _machine(clock)
    with pytest.raises(IllegalTransitionError):
        machine.advance(Stage.DISCOVER)  # not started
    with pytest.raises(IllegalTransitionError):
        machine.finish()
    machine.start()
    with pytest.raises(IllegalTransitionError):
        machine.start()
    machine.advance(Stage.PARSE)
    for stage in (Stage.PARSE, Stage.CLONE):  # sideways, backwards
        with pytest.raises(IllegalTransitionError):
            machine.advance(stage)
    with pytest.raises(IllegalTransitionError):
        machine.update_progress(Stage.SYMBOLS, 1, 2)  # not the running stage
    with pytest.raises(IllegalTransitionError):
        machine.fail(Stage.CLONE, {"code": "x", "message": "x"})
    with pytest.raises(IllegalTransitionError, match="finishes from finalize"):
        machine.finish()


@pytest.mark.parametrize(("done", "total"), [(5, 4), (-1, 4), (0, -1)])
def test_impossible_progress_is_refused(clock: Clock, done: int, total: int) -> None:
    machine = _machine(clock)
    machine.start()
    with pytest.raises(ValueError, match="0 <= done <= total"):
        machine.update_progress(Stage.CLONE, done, total)


def test_progress_never_goes_backwards(clock: Clock) -> None:
    machine = _machine(clock)
    machine.start()
    machine.update_progress(Stage.CLONE, 50, 100)
    with pytest.raises(ValueError, match="backwards"):
        machine.update_progress(Stage.CLONE, 40, 100)


@pytest.mark.parametrize("end", ["succeeded", "failed", "cancelled"])
def test_finished_jobs_stay_finished(clock: Clock, end: str) -> None:
    machine = _machine(clock)
    machine.start()
    if end == "succeeded":
        _run_to(machine, Stage.FINALIZE)
        machine.finish()
    elif end == "failed":
        machine.fail(Stage.CLONE, {"code": "clone_failed", "message": "Repository not found"})
    else:
        machine.cancel()
    for move in (machine.cancel, machine.start, lambda: machine.advance(Stage.FINALIZE)):
        with pytest.raises(IllegalTransitionError):
            move()


# ------------------------------------------------------------------ failure and cancel


def test_failure_is_recorded_on_the_stage_and_the_job(clock: Clock) -> None:
    machine = _machine(clock)
    machine.start()
    _run_to(machine, Stage.PARSE)
    machine.fail(Stage.PARSE, {"code": "parse_crashed", "message": "tree-sitter gave up"})
    assert machine.job.status is JobStatus.FAILED
    assert machine.job.error == {
        "stage": "parse",
        "code": "parse_crashed",
        "message": "tree-sitter gave up",
    }
    assert machine.job.stages["parse"]["status"] == "failed"
    assert machine.job.stages["parse"]["error"]["code"] == "parse_crashed"
    assert machine.job.eta_s is None
    assert machine.events[-1].type == "job.failed"
    # Stages finished before the failure still count.
    assert machine.features() == [Feature.CODE_BROWSER]


def test_cancel_from_queued_or_running(clock: Clock) -> None:
    queued = _machine(clock)
    queued.cancel()
    assert queued.job.status is JobStatus.CANCELLED
    running = _machine(clock)
    running.start()
    running.cancel()
    assert running.job.stages["clone"]["status"] == "cancelled"
    assert running.job.finished_at == clock.now


# ------------------------------------------------------------------ ETA


def test_eta_uses_a_moving_average_of_the_rate(clock: Clock) -> None:
    machine = _machine(clock)
    machine.start()
    _run_to(machine, Stage.PARSE)
    machine.update_progress(Stage.PARSE, 0, 1000)
    assert machine.job.eta_s is None  # no rate yet: unknown, not invented
    clock.tick(10)
    machine.update_progress(Stage.PARSE, 100, 1000)  # 10 files/s
    assert machine.job.eta_s == 90  # 900 left / 10
    clock.tick(10)
    machine.update_progress(Stage.PARSE, 300, 1000)  # 20 files/s now; average 0.3*20 + 0.7*10
    assert machine.job.stages["parse"]["rate"] == pytest.approx(13.0)
    assert machine.job.eta_s == 54  # ceil(700 / 13)


def test_eta_adds_expected_time_for_later_stages(clock: Clock) -> None:
    machine = _machine(clock, expected_seconds={Stage.SYMBOLS: 30, Stage.EMBEDDINGS: 120})
    machine.start()
    _run_to(machine, Stage.PARSE)
    machine.update_progress(Stage.PARSE, 0, 100)
    clock.tick(10)
    machine.update_progress(Stage.PARSE, 50, 100)  # 5/s → 10 s left in parse
    assert machine.job.eta_s == 10 + 30 + 120


def test_jsonb_changes_are_new_objects(clock: Clock) -> None:
    """SQLAlchemy only notices a JSON column changed when it's assigned a new object."""
    machine = _machine(clock)
    machine.start()
    before = machine.job.stages
    machine.update_progress(Stage.CLONE, 1, 2)
    assert machine.job.stages is not before


# ------------------------------------------------------------------ the unlock map


def test_every_feature_is_unlocked_by_exactly_one_stage() -> None:
    features = [feature for unlocks in UNLOCKS.values() for feature in unlocks]
    assert sorted(features) == sorted(Feature)
    assert len(features) == len(set(features))


def test_the_unlock_map_matches_the_plan() -> None:
    assert unlocked([Stage.DISCOVER]) == [Feature.CODE_BROWSER]
    assert unlocked([Stage.SYMBOLS]) == [Feature.GOTO_DEFINITION]
    assert unlocked([Stage.CHUNKS]) == [Feature.KEYWORD_SEARCH]
    assert unlocked([Stage.EMBEDDINGS]) == [Feature.SEMANTIC_SEARCH, Feature.ASK]
    assert unlocked([Stage.SUMMARIES]) == [Feature.WIKI]
    assert unlocked([Stage.GRAPH]) == [Feature.GRAPH_VIEW, Feature.CALLERS]
    assert unlocked([Stage.LINKS]) == [
        Feature.TRACE,
        Feature.DIAGRAMS,
        Feature.FIXES,
        Feature.CHANGES,
    ]
    for stage in (Stage.CLONE, Stage.MERKLE, Stage.PARSE, Stage.FINALIZE):
        assert unlocked([stage]) == []


def test_weights_cover_every_stage() -> None:
    assert set(STAGE_WEIGHTS) == set(Stage)
    assert sum(STAGE_WEIGHTS.values()) == 100
    assert tuple(Stage) == STAGES
