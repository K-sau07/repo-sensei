"""The fix state machine (8.4): stages only move forward, approval between plan and patch,
stops end it once, and each move is an event."""

from __future__ import annotations

import uuid

import pytest

from sensei_core.models import FixRun, FixSource, FixStage, FixStatus
from sensei_fixer.state import FixStateError, FixStateMachine


def _run(status: FixStatus = FixStatus.QUEUED, stage: FixStage = FixStage.INTAKE) -> FixRun:
    return FixRun(
        id=uuid.uuid4(), org_id=uuid.uuid4(), repo_id=uuid.uuid4(), source=FixSource.PASTE,
        title="t", ticket={}, status=status, stage=stage,
    )  # fmt: skip


def _through(machine: FixStateMachine) -> FixStage:
    machine.begin_stage()
    return machine.advance()


def test_stages_move_forward_to_approval_then_to_a_pull_request() -> None:
    run = _run()
    machine = FixStateMachine(run)
    reached = [_through(machine) for _ in range(5)]
    assert reached == [FixStage.INVESTIGATE, FixStage.REPRODUCE, FixStage.ROOT_CAUSE,
                       FixStage.PLAN, FixStage.AWAITING_APPROVAL]  # fmt: skip
    assert run.status is FixStatus.AWAITING_APPROVAL
    with pytest.raises(FixStateError, match="can't begin a stage"):
        machine.begin_stage()  # nothing runs until a person approves
    machine.approve()
    assert (run.stage, run.status) == (FixStage.PATCH, FixStatus.QUEUED)
    assert [_through(machine) for _ in range(3)] == [FixStage.VERIFY, FixStage.PR, FixStage.DONE]
    assert run.status is FixStatus.SUCCEEDED
    assert run.finished_at is not None
    kinds = [e["type"] for e in machine.events]
    assert kinds.count("stage_started") == 8
    assert "awaiting_approval" in kinds
    assert kinds[-1] == "done"
    assert {e["fix_id"] for e in machine.events} == {str(run.id)}


def test_an_edited_plan_is_planned_again_then_back_to_approval() -> None:
    run = _run(FixStatus.AWAITING_APPROVAL, FixStage.AWAITING_APPROVAL)
    machine = FixStateMachine(run)
    machine.replan()
    assert (run.stage, run.status) == (FixStage.PLAN, FixStatus.QUEUED)
    assert _through(machine) is FixStage.AWAITING_APPROVAL


def test_a_stop_ends_it_once_with_its_reason() -> None:
    run = _run(FixStatus.RUNNING, FixStage.REPRODUCE)
    machine = FixStateMachine(run)
    machine.stop(FixStatus.NOT_REPRODUCED, "The test passed three times.")
    assert (run.status, run.stop_reason) == (
        FixStatus.NOT_REPRODUCED,
        "The test passed three times.",
    )
    assert machine.events[-1]["type"] == "not_reproduced"
    with pytest.raises(FixStateError):
        machine.stop(FixStatus.CANCELLED, "too late")
    with pytest.raises(FixStateError, match="isn't a way to stop"):
        FixStateMachine(_run()).stop(FixStatus.SUCCEEDED, "no")


def test_triage_waits_for_a_person_to_start_it() -> None:
    run = _run(FixStatus.NEEDS_TRIAGE)
    machine = FixStateMachine(run)
    with pytest.raises(FixStateError):
        machine.begin_stage()
    machine.start()
    assert run.status is FixStatus.QUEUED
    with pytest.raises(FixStateError, match="can't start it"):
        machine.start()
