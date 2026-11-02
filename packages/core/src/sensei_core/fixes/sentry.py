"""Sentry as a fix's source (8.3): an org's internal integration sends its webhooks here.

- :func:`signature_valid`: ``Sentry-Hook-Signature`` is the HMAC-SHA256 (hex) of the raw body
  with the integration's client secret.
- :func:`parse_webhook`: an ``issue`` (created) or ``event_alert`` (triggered) delivery as the
  issue it is about, with the project it belongs to (which says the repository).
- :func:`ticket_from_event`: a Sentry event's exception and frames as a ticket. Both shapes are
  read: the webhook's (``exception.values``) and the API's (``entries`` of type exception).
- :func:`should_auto_run`: the gate. A fix starts by itself only for an issue that is busy
  (at least ``min_events`` events in the last 14 days) and current (seen in that window), and
  only where the org turned that on; otherwise it waits in the inbox for someone to decide.
- :class:`SentryClient`: the issue's events over 14 days and its latest event, with the
  integration's token.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import httpx

from sensei_core.fixes.tickets import Frame, PathIndex, Ticket, parse_ticket

WINDOW = timedelta(days=14)
MIN_EVENTS = 10
TIMEOUT = httpx.Timeout(10.0)
_PROJECT_IN_URL = re.compile(r"/projects/[^/]+/(?P<slug>[^/]+)/")

Json = dict[str, Any]


def signature_valid(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


@dataclass(frozen=True, slots=True)
class SentryIssue:
    id: str
    title: str
    project: str | None  # the project's slug
    url: str | None
    event: Json | None  # the event that came with it (event alerts), if any
    count: int | None
    last_seen: datetime | None


def _obj(value: object) -> Json:
    return cast("Json", value) if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return cast("list[Any]", value) if isinstance(value, list) else []


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_webhook(resource: str | None, payload: Json) -> SentryIssue | None:
    """The issue a delivery is about (None: a delivery that isn't about a new or alerting
    issue, such as a resolved one or the installation's)."""
    data = _obj(payload.get("data"))
    action = payload.get("action")
    if resource == "issue" and action == "created":
        issue = _obj(data.get("issue"))
        if not issue.get("id"):
            return None
        count = issue.get("count")
        return SentryIssue(
            id=str(issue["id"]),
            title=str(issue.get("title") or "Sentry issue"),
            project=_obj(issue.get("project")).get("slug"),
            url=issue.get("web_url") or issue.get("permalink"),
            event=None,
            count=int(count) if isinstance(count, int | str) and str(count).isdigit() else None,
            last_seen=_when(issue.get("lastSeen")),
        )
    if resource == "event_alert" and action == "triggered":
        event = _obj(data.get("event"))
        issue_id = event.get("issue_id")
        if not issue_id:
            return None
        project = _PROJECT_IN_URL.search(str(event.get("url") or ""))
        return SentryIssue(
            id=str(issue_id),
            title=str(event.get("title") or "Sentry issue"),
            project=project["slug"] if project else None,
            url=event.get("web_url"),
            event=event,
            count=None,
            last_seen=_when(event.get("datetime")),
        )
    return None


def _exceptions(event: Json) -> list[Json]:
    values = _list(_obj(event.get("exception")).get("values"))
    if not values:
        for entry in _list(event.get("entries")):
            found = _obj(entry)
            if found.get("type") == "exception":
                values = _list(_obj(found.get("data")).get("values"))
                break
    return [_obj(v) for v in values]


def ticket_from_event(event: Json, *, title: str, paths: Iterable[str] = ()) -> Ticket:
    """The event's last exception (the one raised) and its frames, oldest call first."""
    exceptions = _exceptions(event)
    if not exceptions:
        return parse_ticket(str(event.get("message") or title), title=title, paths=paths)
    raised = exceptions[-1]
    index = PathIndex(paths)
    frames: list[Frame] = []
    for raw in _list(_obj(raised.get("stacktrace")).get("frames")):
        frame = _obj(raw)
        file = str(frame.get("abs_path") or frame.get("filename") or "")
        line = frame.get("lineno")
        if not file or not isinstance(line, int):
            continue
        path = index.match(file) if frame.get("in_app") is not False else None
        frames.append(
            Frame(
                file=file, line=line, function=frame.get("function"), path=path,
                in_repo=path is not None, library=frame.get("in_app") is False,
            )
        )  # fmt: skip
    platform = str(event.get("platform") or "")
    language = (
        "python" if platform == "python"
        else "javascript" if platform in ("javascript", "node")
        else None
    )  # fmt: skip
    return Ticket(
        title=title[:200],
        language=language,
        error_type=raised.get("type"),
        message=raised.get("value"),
        frames=frames,
        text=f"{raised.get('type') or 'Error'}: {raised.get('value') or ''}".strip(),
    )


def should_auto_run(
    *,
    enabled: bool,
    events_14d: int | None,
    last_seen: datetime | None,
    now: datetime,
    min_events: int = MIN_EVENTS,
) -> bool:
    if not enabled or events_14d is None or last_seen is None:
        return False
    return events_14d >= min_events and now - last_seen <= WINDOW


class SentryClient:
    """Sentry's API for one integration (its organization, its token)."""

    def __init__(
        self, http: httpx.AsyncClient, *, api_url: str, organization: str, token: str
    ) -> None:
        self._http = http
        self._base = f"{api_url.rstrip('/')}/api/0/organizations/{organization}"
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _get(self, path: str, **params: str) -> Json | None:
        try:
            response = await self._http.get(
                self._base + path, params=params, headers=self._headers, timeout=TIMEOUT
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        return _obj(response.json())

    async def events_14d(self, issue_id: str) -> int | None:
        """How many events the issue had in the last 14 days (None: Sentry didn't say)."""
        found = await self._get(f"/issues/{issue_id}/", statsPeriod="14d")
        if found is None:
            return None
        buckets = _list(_obj(found.get("stats")).get("14d"))
        try:
            return sum(int(_list(bucket)[1]) for bucket in buckets)
        except (IndexError, TypeError, ValueError):
            return None

    async def latest_event(self, issue_id: str) -> Json | None:
        return await self._get(f"/issues/{issue_id}/events/latest/")
