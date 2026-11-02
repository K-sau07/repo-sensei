"""A fix's stages for workers (8.4): the stages that run a repository's code go to the sandbox
queue, the others to the agents'; each message names its unit of work (the fix and stage)."""

from __future__ import annotations

import uuid

from sensei_core.jobs import Queue, TaskName, fix_queue, fix_stage_message
from sensei_core.models import FixStage


def test_sandbox_stages_go_to_the_sandbox_queue() -> None:
    sandboxed = {s for s in FixStage if fix_queue(s.value) is Queue.SANDBOX}
    assert sandboxed == {FixStage.REPRODUCE, FixStage.PATCH, FixStage.VERIFY}
    assert fix_queue("investigate") is Queue.AGENT


def test_a_stage_message_names_the_fix_and_the_stage() -> None:
    org, fix = uuid.uuid4(), uuid.uuid4()
    message = fix_stage_message(org, fix, "reproduce")
    assert message == {
        "name": TaskName.RUN_FIX_STAGE.value,
        "kwargs": {"org_id": str(org), "fix_id": str(fix), "stage": "reproduce"},
        "queue": "sandbox",
    }
