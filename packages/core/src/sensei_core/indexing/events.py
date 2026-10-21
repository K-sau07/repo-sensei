"""Index progress as events: what the browser streams while a repository is indexed.

Every change the state machine makes becomes an event on the job's Redis Stream
``jobs:{job_id}`` (capped at about 10,000 entries, kept for a week), which the SSE endpoint
replays from any point (``Last-Event-ID`` is the stream entry id). Job-level changes (queued,
started, done, failed, cancelled) also go on the org's stream ``org:{org_id}:events``, for the
sidebar dots and the inbox. Payloads::

    {"type": "progress", "job_id": "…", "repo_id": "…", "stage": "discover",
     "done": 500, "total": 2000, "eta_s": 12, "ts": "2026-10-20T12:00:00+00:00"}

Types: ``queued``, ``stage_started``, ``progress``, ``stage_done``, ``unlock`` (with
``unlocks``: the features now available), ``log`` (``level``, ``message``), ``constellation``
(``cursor``: how many of the run's constellation points can be read now, of ``total``, and the
``generation`` they belong to; the points themselves come from the constellation endpoint), and
the final ``done``, ``failed`` (``code``, ``message``) or ``cancelled``.

Events are collected while the job row is locked and published after the transaction commits
(:class:`JobOutbox`), so a listener never hears about a change that was rolled back.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import orjson
from redis.asyncio import Redis

from sensei_core.indexing.state import IndexJobStateMachine, JobEvent
from sensei_core.models import IndexJob

JOB_STREAM_MAXLEN = 10_000
ORG_STREAM_MAXLEN = 1_000
STREAM_TTL_SECONDS = 7 * 86_400


class EventType(enum.StrEnum):
    QUEUED = "queued"
    STAGE_STARTED = "stage_started"
    PROGRESS = "progress"
    STAGE_DONE = "stage_done"
    UNLOCK = "unlock"
    LOG = "log"
    CONSTELLATION = "constellation"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({EventType.DONE, EventType.FAILED, EventType.CANCELLED})
# Org-wide events: the job's lifecycle, not its progress.
ORG_TYPES = frozenset({EventType.QUEUED, EventType.STAGE_STARTED, *TERMINAL})

_MACHINE_TYPES: dict[str, EventType | None] = {
    "job.started": None,  # the first stage_started says it
    "stage.started": EventType.STAGE_STARTED,
    "stage.progress": EventType.PROGRESS,
    "stage.finished": EventType.STAGE_DONE,
    "job.log": EventType.LOG,
    "job.succeeded": EventType.DONE,
    "job.failed": EventType.FAILED,
    "job.cancelled": EventType.CANCELLED,
}


def job_stream(job_id: uuid.UUID | str) -> str:
    return f"jobs:{job_id}"


def org_stream(org_id: uuid.UUID | str) -> str:
    return f"org:{org_id}:events"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _payload(job: IndexJob, kind: EventType, event: JobEvent | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": kind.value,
        "job_id": str(job.id),
        "repo_id": str(job.repo_id),
        "stage": stage.value if (stage := (event.stage if event else None) or job.stage) else None,
        "ts": _now(),
    }
    data = event.data if event else {}
    if kind is EventType.PROGRESS:
        payload.update(done=data.get("done"), total=data.get("total"), eta_s=job.eta_s)
    elif kind is EventType.LOG:
        payload.update(level=data.get("level"), message=data.get("message"))
    elif kind is EventType.FAILED:
        error = cast("dict[str, Any]", data.get("error") or {})
        payload.update(code=error.get("code"), message=error.get("message"))
    if kind in (EventType.STAGE_STARTED, EventType.STAGE_DONE, EventType.PROGRESS):
        payload["progress"] = job.progress
    return payload


def payloads_for(job: IndexJob, events: Iterable[JobEvent]) -> list[dict[str, Any]]:
    """The wire events for what the state machine just did (read the job before commit)."""
    out: list[dict[str, Any]] = []
    for event in events:
        kind = _MACHINE_TYPES.get(event.type)
        if kind is None:
            continue
        out.append(_payload(job, kind, event))
        unlocks = event.data.get("unlocks") if kind is EventType.STAGE_DONE else None
        if unlocks:
            unlock = _payload(job, EventType.UNLOCK, event)
            unlock["unlocks"] = list(unlocks)
            out.append(unlock)
    return out


@dataclass(slots=True)
class JobOutbox:
    """Events waiting for the transaction to commit. ``collect`` inside it, ``publish`` after."""

    # (job id, org id, payload)
    items: list[tuple[str, str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, str, dict[str, Any]]]
    )

    def collect(self, machine: IndexJobStateMachine) -> None:
        job = machine.job
        for payload in payloads_for(job, machine.events):
            self.items.append((str(job.id), str(job.org_id), payload))
        machine.events.clear()

    def queued(self, job: IndexJob) -> None:
        self.items.append((str(job.id), str(job.org_id), _payload(job, EventType.QUEUED, None)))

    def constellation(self, job: IndexJob, *, cursor: int, total: int, generation: str) -> None:
        """More of the run's constellation points can be read (up to ``cursor``)."""
        payload = _payload(job, EventType.CONSTELLATION, None)
        payload.update(cursor=cursor, total=total, generation=generation)
        self.items.append((str(job.id), str(job.org_id), payload))

    async def publish(self, redis: Redis) -> None:
        if not self.items:
            return
        async with redis.pipeline(transaction=False) as pipe:
            for job_id, org_id, payload in self.items:
                data = orjson.dumps(payload).decode()
                pipe.xadd(
                    job_stream(job_id),
                    {"data": data},
                    maxlen=JOB_STREAM_MAXLEN,
                    approximate=True,
                )
                pipe.expire(job_stream(job_id), STREAM_TTL_SECONDS)
                if payload["type"] in ORG_TYPES:
                    pipe.xadd(
                        org_stream(org_id),
                        {"data": data},
                        maxlen=ORG_STREAM_MAXLEN,
                        approximate=True,
                    )
                    pipe.expire(org_stream(org_id), STREAM_TTL_SECONDS)
            await cast("Awaitable[object]", pipe.execute())  # pyright: ignore[reportUnknownMemberType]
        self.items.clear()


def parse_entry(entry_id: str, fields: dict[str, str]) -> dict[str, Any]:
    """A stream entry back into its payload, with the entry id as ``id``."""
    payload = cast("dict[str, Any]", orjson.loads(fields["data"]))
    payload["id"] = entry_id
    return payload
