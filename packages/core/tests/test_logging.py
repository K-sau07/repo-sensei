from __future__ import annotations

import json
import logging

import pytest

from sensei_core.config import Settings
from sensei_core.context import bind_context, current_context
from sensei_core.logging import configure_logging, get_logger

GH_TOKEN = "ghs_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"


def _json_lines(err: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in err.splitlines() if line.startswith("{")]


def test_json_logs_include_context_and_redact(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(env="prod"), force_json=True)
    log = get_logger("test")

    with bind_context(request_id="req-1", org_id="org-9"):
        log.info("cloning repo", url=f"https://x-access-token:{GH_TOKEN}@github.com/a/b.git")

    lines = _json_lines(capsys.readouterr().err)
    assert len(lines) == 1
    line = lines[0]
    assert line["event"] == "cloning repo"
    assert line["request_id"] == "req-1"
    assert line["org_id"] == "org-9"
    assert line["level"] == "info"
    assert GH_TOKEN not in json.dumps(line)


def test_context_is_restored_after_block() -> None:
    with bind_context(job_id="job-1"):
        assert current_context()["job_id"] == "job-1"
    assert "job_id" not in current_context()


def test_unknown_context_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown context keys"), bind_context(colour="blue"):
        pass


def test_stdlib_loggers_go_through_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(env="prod"), force_json=True)
    logging.getLogger("uvicorn.error").warning("token=%s", GH_TOKEN)
    lines = _json_lines(capsys.readouterr().err)
    assert lines
    assert GH_TOKEN not in json.dumps(lines[-1])
    assert lines[-1]["level"] == "warning"
