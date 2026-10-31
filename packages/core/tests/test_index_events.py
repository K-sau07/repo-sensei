"""What the state machine's moves look like on the wire."""

from __future__ import annotations

import uuid

import pytest

from sensei_core.indexing.events import MAX_CHANGED, ORG_TYPES, JobOutbox, payloads_for
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.models import IndexJob, JobKind, JobStatus, Repo, Stage


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


def test_the_org_stream_gets_the_lifecycle_and_a_repository_moving_on() -> None:
    assert {t.value for t in ORG_TYPES} == {
        "queued",
        "stage_started",
        "done",
        "failed",
        "cancelled",
        "repo_updated",  # a ref's index moved on (5.5)
    }


def test_constellation_events_say_how_far_points_can_be_read() -> None:
    machine = _machine()
    machine.start()
    machine.advance(Stage.DISCOVER)
    outbox = JobOutbox()
    outbox.constellation(machine.job, cursor=500, total=1200, generation="g1")
    [(job_id, org_id, payload)] = outbox.items
    assert (job_id, org_id) == (str(machine.job.id), str(machine.job.org_id))
    assert (payload["type"], payload["stage"]) == ("constellation", "discover")
    assert (payload["cursor"], payload["total"], payload["generation"]) == (500, 1200, "g1")
    assert "constellation" not in {t.value for t in ORG_TYPES}  # the run's own stream only


def test_a_repository_moving_on_names_its_ref_commit_and_changed_files() -> None:
    machine = _machine()
    outbox = JobOutbox()
    repo = Repo(id=machine.job.repo_id, org_id=machine.job.org_id)
    outbox.repo_updated(
        repo,
        job=machine.job,
        ref="main",
        sha="b" * 40,
        changed=[f"f{n}.py" for n in range(MAX_CHANGED + 3)],
    )
    outbox.repo_updated(repo, job=None, ref="web", sha="c" * 40, changed=None)  # the ref moved
    (job_id, _, many), (no_job, org_id, full) = outbox.items
    assert (job_id, many["job_id"]) == (str(machine.job.id), str(machine.job.id))
    assert (no_job, full["job_id"], org_id) == ("", None, str(machine.job.org_id))
    assert (many["type"], many["ref"], many["sha"]) == ("repo_updated", "main", "b" * 40)
    assert len(many["changed_files"]) == MAX_CHANGED  # named up to a point, counted in full
    assert many["changed_count"] == MAX_CHANGED + 3
    assert (full["changed_files"], full["changed_count"]) == (None, None)  # read in full
