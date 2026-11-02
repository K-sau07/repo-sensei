"""Sentry as a fix's source (8.3): the webhook's signature, its deliveries as issues, events as
tickets, the auto-run gate, and the issue's last 14 days from Sentry's API."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from sensei_core.fixes.sentry import (
    SentryClient,
    parse_webhook,
    should_auto_run,
    signature_valid,
    ticket_from_event,
)

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
PATHS = ["backend/app/services/pricing.py", "backend/app/api/routes/orders.py"]
FRAMES = [
    {"filename": "starlette/routing.py", "abs_path": "/usr/lib/python3.12/site-packages/"
     "starlette/routing.py", "lineno": 73, "function": "app", "in_app": False},
    {"filename": "app/api/routes/orders.py", "abs_path": "/srv/backend/app/api/routes/orders.py",
     "lineno": 31, "function": "create_order", "in_app": True},
    {"filename": "app/services/pricing.py", "abs_path": "/srv/backend/app/services/pricing.py",
     "lineno": 12, "function": "unit_price", "in_app": True},
]  # fmt: skip
EXCEPTION = {"values": [{"type": "ZeroDivisionError", "value": "division by zero",
                         "stacktrace": {"frames": FRAMES}}]}  # fmt: skip


def test_the_signature_is_the_bodys_hmac_with_the_client_secret() -> None:
    body = b'{"action":"created"}'
    good = hmac.new(b"s3cret-client", body, hashlib.sha256).hexdigest()
    assert signature_valid("s3cret-client", body, good)
    assert not signature_valid("s3cret-client", body + b" ", good)
    assert not signature_valid("other-secret", body, good)
    assert not signature_valid("s3cret-client", body, None)
    assert not signature_valid("", body, good)


def test_deliveries_about_new_or_alerting_issues_are_read_and_others_dropped() -> None:
    created = parse_webhook("issue", {"action": "created", "data": {"issue": {
        "id": "4501", "title": "ZeroDivisionError: division by zero", "count": "37",
        "lastSeen": "2026-11-01T09:00:00Z", "project": {"slug": "backend"},
        "web_url": "https://acme.sentry.io/issues/4501/"}}})  # fmt: skip
    assert created is not None
    assert (created.id, created.project, created.count) == ("4501", "backend", 37)
    assert created.last_seen == datetime(2026, 11, 1, 9, tzinfo=UTC)
    alert = parse_webhook("event_alert", {"action": "triggered", "data": {"event": {
        "issue_id": "4501", "title": "ZeroDivisionError", "datetime": "2026-11-02T10:00:00Z",
        "url": "https://sentry.io/api/0/projects/acme/backend/events/abc/",
        "exception": EXCEPTION}}})  # fmt: skip
    assert alert is not None
    assert (alert.id, alert.project) == ("4501", "backend")
    assert alert.event is not None
    assert parse_webhook("issue", {"action": "resolved", "data": {"issue": {"id": "1"}}}) is None
    assert parse_webhook("installation", {"action": "created", "data": {}}) is None
    assert parse_webhook("event_alert", {"action": "triggered", "data": {"event": {}}}) is None


@pytest.mark.parametrize(
    "event",
    [
        {"platform": "python", "exception": EXCEPTION},  # the webhook's shape
        {"platform": "python", "entries": [{"type": "exception", "data": EXCEPTION}]},  # the API's
    ],
)
def test_an_events_raised_exception_becomes_the_ticket(event: dict[str, object]) -> None:
    ticket = ticket_from_event(event, title="ZeroDivisionError", paths=PATHS)
    assert (ticket.language, ticket.error_type, ticket.message) == (
        "python", "ZeroDivisionError", "division by zero",
    )  # fmt: skip
    assert [f.path for f in ticket.frames] == [
        None, "backend/app/api/routes/orders.py", "backend/app/services/pricing.py",
    ]  # fmt: skip
    assert ticket.frames[0].library
    culprit = ticket.culprit
    assert culprit is not None
    assert (culprit.path, culprit.line, culprit.function) == (
        "backend/app/services/pricing.py", 12, "unit_price",
    )  # fmt: skip


def test_an_event_without_an_exception_is_still_a_ticket() -> None:
    ticket = ticket_from_event({"message": "Payment webhook timed out"}, title="Timeout")
    assert ticket.title == "Timeout"
    assert ticket.frames == []


@pytest.mark.parametrize(
    ("enabled", "events", "seen_ago", "expected"),
    [
        (True, 10, timedelta(hours=1), True),
        (True, 9, timedelta(hours=1), False),  # not busy enough
        (True, 500, timedelta(days=15), False),  # not current
        (True, 500, timedelta(days=14), True),
        (False, 500, timedelta(hours=1), False),  # the org didn't turn it on
        (True, None, timedelta(hours=1), False),  # Sentry didn't say: a person decides
    ],
)
def test_the_gate_starts_only_busy_current_issues_where_it_is_on(
    enabled: bool, events: int | None, seen_ago: timedelta, expected: bool
) -> None:
    got = should_auto_run(enabled=enabled, events_14d=events, last_seen=NOW - seen_ago, now=NOW)
    assert got is expected


async def test_sentry_says_the_issues_last_14_days_and_its_latest_event() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/events/latest/"):
            return httpx.Response(200, json={"platform": "python", "exception": EXCEPTION})
        if request.url.path == "/api/0/organizations/acme/issues/4501/":
            return httpx.Response(200, json={"stats": {"14d": [[1, 4], [2, 0], [3, 9]]}})
        return httpx.Response(404, json={"detail": "not found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        client = SentryClient(http, api_url="https://sentry.io/", organization="acme",
                              token="sntrys_token_value")  # fmt: skip
        assert await client.events_14d("4501") == 13
        assert await client.latest_event("4501") is not None
        assert await client.events_14d("9999") is None
    assert seen[0].url.params["statsPeriod"] == "14d"
    assert seen[0].headers["authorization"] == "Bearer sntrys_token_value"


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://sentry.io", True),
        ("https://acme.sentry.io/", True),
        ("https://de.sentry.io:443", True),
        ("http://sentry.io", False),
        ("https://sentry.io.evil.com", False),
        ("https://evilsentry.io", False),
        ("https://10.0.0.5", False),
        ("https://user:pw@sentry.io", False),
        ("https://sentry.io:8443", False),
        ("not a url", False),
    ],
)
def test_sentry_is_asked_only_on_the_allowed_hosts(url: str, ok: bool) -> None:
    from sensei_core.fixes.sentry import host_allowed  # noqa: PLC0415

    assert host_allowed(url, ["sentry.io"]) is ok
