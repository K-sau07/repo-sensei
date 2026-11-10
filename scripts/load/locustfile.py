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
Every virtual user is the key's one person, and search allows 60 a minute a person: on the
target, raise ``SENSEI_SEARCH_RATE_PER_MINUTE`` for the run (or give each user a key of its
own), or the run measures the limit. A refusal is counted as its own line (``… 429``), apart
from the searches' latency.
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
        with self.client.post(
            f"{BASE}/search", json=body, name="POST search (keyword)", catch_response=True
        ) as response:
            if response.status_code == 429:
                response.failure("429: the per-person search limit, not the search")

    @task(2)
    def neighbors(self) -> None:
        if not SYMBOL:
            return
        self.client.get(
            f"{BASE}/graph/neighbors",
            params={"symbol": SYMBOL, "direction": "both", "depth": 1},
            name="GET graph/neighbors",
        )


SESSION = os.environ.get("SENSEI_LOAD_SESSION", "")


if SESSION:  # Locust runs the user classes the file defines: this one only with a session

    class Watcher(HttpUser):
        """An open tab: the org's live events, held for a while, then opened again. Only with a
        session (``SENSEI_LOAD_SESSION``): without one, no users of this kind."""

        wait_time = between(1, 2)

        def on_start(self) -> None:
            # The session cookie's name: __Host-session over HTTPS, session in development.
            name = os.environ.get("SENSEI_LOAD_SESSION_COOKIE", "__Host-session")
            self.client.cookies.set(name, SESSION)

        @task
        def events(self) -> None:
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
