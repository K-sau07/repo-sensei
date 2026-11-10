"""Load scenarios (10.11): what people do most, against a running API, as an API key's user.

Reading code (the tree, a file, symbols), keyword search (no embedding call: a run costs
nothing), the graph one step at a time, and the org's live events held open (SSE fan-out: a
browser's, so it takes a signed-in session's cookie, ``SENSEI_LOAD_SESSION``; keys can't).
Ask isn't here: its time is the model's, and every run of it is paid; it is measured on its own,
against a stub model (``SENSEI_ANTHROPIC_BASE_URL``), not through this file.

    SENSEI_LOAD_KEY=rs_… SENSEI_LOAD_ORG=acme SENSEI_LOAD_REPO=api \\
    SENSEI_LOAD_FILE=backend/app/main.py SENSEI_LOAD_SYMBOL='sym:backend/app/main.py::app' \\
    uvx locust==2.47.0 -f scripts/load/locustfile.py --host https://staging.example \\
        --headless -u 50 -r 5 -t 5m --csv load

Every request is named by its route (not its arguments), so the report has one line each.
"""

from __future__ import annotations

import os
import random
import time

from locust import HttpUser, between, task

ORG = os.environ.get("SENSEI_LOAD_ORG", "acme")
REPO = os.environ.get("SENSEI_LOAD_REPO", "api")
FILE = os.environ.get("SENSEI_LOAD_FILE", "README.md")
SYMBOL = os.environ.get("SENSEI_LOAD_SYMBOL", "")
QUERIES = os.environ.get("SENSEI_LOAD_QUERIES", "create item,login,password,settings").split(",")
EVENTS_SECONDS = float(os.environ.get("SENSEI_LOAD_EVENTS_SECONDS", "20"))
BASE = f"/api/v1/orgs/{ORG}/repos/{REPO}"


def _headers() -> dict[str, str]:
    key = os.environ.get("SENSEI_LOAD_KEY", "")
    if not key:
        msg = "Set SENSEI_LOAD_KEY to an API key (read:repo) of a member of the org."
        raise SystemExit(msg)
    return {"Authorization": f"Bearer {key}"}


class Reader(HttpUser):
    """Someone reading and searching a repository: a few seconds between steps."""

    wait_time = between(1, 4)

    def on_start(self) -> None:
        self.client.headers.update(_headers())

    @task(4)
    def tree(self) -> None:
        self.client.get(f"{BASE}/tree", name="GET tree")

    @task(4)
    def file(self) -> None:
        self.client.get(f"{BASE}/file", params={"path": FILE}, name="GET file")

    @task(3)
    def symbols(self) -> None:
        q = random.choice(QUERIES).split()[0]  # noqa: S311 - not for security
        self.client.get(f"{BASE}/symbols", params={"q": q, "limit": 20}, name="GET symbols")

    @task(3)
    def search(self) -> None:
        body = {"q": random.choice(QUERIES), "mode": "keyword", "limit": 20}  # noqa: S311
        self.client.post(f"{BASE}/search", json=body, name="POST search (keyword)")

    @task(2)
    def neighbors(self) -> None:
        if not SYMBOL:
            return
        self.client.get(
            f"{BASE}/graph/neighbors",
            params={"symbol": SYMBOL, "direction": "both", "depth": 1},
            name="GET graph/neighbors",
        )


class Watcher(HttpUser):
    """An open tab: the org's live events, held for a while, then opened again."""

    wait_time = between(1, 2)
    weight = 1

    def on_start(self) -> None:
        # The session cookie's name: __Host-session over HTTPS, session in development.
        session = os.environ.get("SENSEI_LOAD_SESSION", "")
        name = os.environ.get("SENSEI_LOAD_SESSION_COOKIE", "__Host-session")
        self.signed_in = bool(session)
        if self.signed_in:
            self.client.cookies.set(name, session)

    @task
    def events(self) -> None:
        if not self.signed_in:
            time.sleep(EVENTS_SECONDS)  # no session given: this scenario is left out
            return
        started = time.monotonic()
        with self.client.get(
            f"/api/v1/orgs/{ORG}/events", stream=True, name="SSE events", catch_response=True
        ) as response:
            if response.status_code != 200:
                response.failure(f"status {response.status_code}")
                return
            for _ in response.iter_lines():
                if time.monotonic() - started > EVENTS_SECONDS:
                    break
            response.success()
