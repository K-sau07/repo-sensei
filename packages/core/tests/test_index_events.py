"""What the state machine's moves look like on the wire."""

from __future__ import annotations

import uuid

import pytest

from sensei_core.indexing.events import ORG_TYPES, payloads_for
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.models import IndexJob, JobKind, JobStatus, Stage


def _machine() -> IndexJobStateMachine:
    job = IndexJob(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        repo_id=uuid.uuid4(),
        kind=JobKind.FULL,
        status=JobStatus.QUEUED,
        stages={},
        progress=0.0,
    )
    return IndexJobStateMachine(job)


def test_moves_become_typed_events() -> None:
    machine = _machine()
    machine.start()
    machine.update_progress(Stage.CLONE, 2, 5)
    machine.advance(Stage.DISCOVER)
    machine.log("Found 3 files.", level="warn")
    machine.advance(Stage.FINALIZE)
    machine.finish()
    events = payloads_for(machine.job, machine.events)
    assert [(e["type"], e["stage"]) for e in events] == [
        ("stage_started", "clone"),
        ("progress", "clone"),
        ("stage_done", "clone"),
        ("stage_started", "discover"),
        ("log", "discover"),
        ("stage_done", "discover"),
        ("unlock", "discover"),
        ("stage_started", "finalize"),
        ("stage_done", "finalize"),
        ("done", "finalize"),
    ]
    progress = events[1]
    assert (progress["done"], progress["total"]) == (2, 5)
    assert events[4]["level"] == "warn"
    assert events[6]["unlocks"] == ["code_browser"]
    assert all(e["job_id"] == str(machine.job.id) for e in events)


def test_a_failure_carries_its_reason() -> None:
    machine = _machine()
    machine.start()
    machine.fail(Stage.CLONE, {"code": "repo_too_large", "message": "Too big."})
    failed = payloads_for(machine.job, machine.events)[-1]
    assert (failed["type"], failed["code"], failed["message"]) == (
        "failed",
        "repo_too_large",
        "Too big.",
    )


def test_log_levels_are_checked() -> None:
    with pytest.raises(ValueError, match="log level"):
        _machine().log("x", level="debug")


def test_the_org_stream_gets_the_lifecycle_only() -> None:
    assert {t.value for t in ORG_TYPES} == {
        "queued",
        "stage_started",
        "done",
        "failed",
        "cancelled",
    }
