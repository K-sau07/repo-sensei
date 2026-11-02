"""Fix intake (8.3): stack traces as people paste them (Python, Node, browsers), parsed into a
ticket whose frames point at the repository's files."""

from __future__ import annotations

from sensei_core.fixes.tickets import PathIndex, parse_ticket

REPO = [
    "backend/app/api/routes/items.py",
    "backend/app/crud.py",
    "backend/app/models.py",
    "frontend/src/routes/items.tsx",
    "frontend/src/client/core/request.ts",
    "server/src/routes/orders.js",
    "server/src/services/pricing.js",
    "web/src/utils.ts",
    "admin/src/utils.ts",
]

PYTHON = """\
INFO:     127.0.0.1:53422 - "POST /api/v1/items/ HTTP/1.1" 500 Internal Server Error
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "/app/.venv/lib/python3.12/site-packages/sqlalchemy/engine/base.py", line 1967, in _exec
    self.dialect.do_execute(
psycopg.errors.NotNullViolation: null value in column "owner_id" violates not-null constraint

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/app/.venv/lib/python3.12/site-packages/starlette/routing.py", line 73, in app
    response = await f(request)
  File "/app/backend/app/api/routes/items.py", line 61, in create_item
    item = crud.create_item(session=session, item_in=item_in, owner_id=None)
  File "/app/backend/app/crud.py", line 52, in create_item
    session.commit()
  File "/app/.venv/lib/python3.12/site-packages/sqlalchemy/orm/session.py", line 2017, in commit
    trans.commit(_to_root=True)
sqlalchemy.exc.IntegrityError: (psycopg.errors.NotNullViolation) null value in column "owner_id"
"""

NODE = """\
TypeError: Cannot read properties of undefined (reading 'price')
    at computeTotal (/srv/app/server/src/services/pricing.js:42:31)
    at Array.reduce (<anonymous>)
    at /srv/app/server/src/routes/orders.js:18:22
    at Layer.handle [as handle_request] (/srv/app/node_modules/express/lib/router/layer.js:95:5)
    at process.processTicksAndRejections (node:internal/process/task_queues:95:5)
"""

CHROME = """\
Uncaught (in promise) TypeError: Cannot read properties of null (reading 'map')
    at ItemsTable (webpack-internal:///(app-pages-browser)/./src/routes/items.tsx:88:27)
    at renderWithHooks (webpack-internal:///(app-pages-browser)/./node_modules/react-dom/cjs/react-dom.development.js:11121:18)
    at https://app.example.com/_next/static/chunks/app/page-7f3a.js:1:2345
"""

FIREFOX = """\
TypeError: data is undefined
request@http://localhost:5173/src/client/core/request.ts?t=1730000000:120:9
@http://localhost:5173/src/routes/items.tsx:40:3
"""


def test_a_chained_python_traceback_is_the_last_exception_with_repo_frames() -> None:
    ticket = parse_ticket(PYTHON, paths=REPO)
    assert ticket.language == "python"
    assert ticket.error_type == "sqlalchemy.exc.IntegrityError"
    assert ticket.message is not None
    assert ticket.message.startswith("(psycopg.errors.NotNullViolation)")
    assert [(f.path, f.line, f.function) for f in ticket.frames if f.in_repo] == [
        ("backend/app/api/routes/items.py", 61, "create_item"),
        ("backend/app/crud.py", 52, "create_item"),
    ]
    assert all(f.library for f in ticket.frames if not f.in_repo)
    culprit = ticket.culprit
    assert culprit is not None
    assert (culprit.path, culprit.line) == ("backend/app/crud.py", 52)
    assert ticket.versions == {"python": "3.12"}
    assert ticket.title.startswith("sqlalchemy.exc.IntegrityError: (psycopg")


def test_a_node_stack_reads_innermost_first_and_is_kept_outermost_first() -> None:
    ticket = parse_ticket(NODE, paths=REPO)
    assert ticket.language == "javascript"
    assert (ticket.error_type, ticket.message) == (
        "TypeError",
        "Cannot read properties of undefined (reading 'price')",
    )
    in_repo = [(f.path, f.line) for f in ticket.frames if f.in_repo]
    assert in_repo == [("server/src/routes/orders.js", 18), ("server/src/services/pricing.js", 42)]
    culprit = ticket.culprit
    assert culprit is not None
    assert culprit.function == "computeTotal"
    express = next(f for f in ticket.frames if "express" in f.file)
    assert express.library
    assert not express.in_repo


def test_browser_stacks_map_webpack_and_dev_server_urls() -> None:
    chrome = parse_ticket(CHROME, paths=REPO)
    assert (chrome.error_type, chrome.message) == (
        "TypeError",
        "Cannot read properties of null (reading 'map')",
    )
    culprit = chrome.culprit
    assert culprit is not None
    assert (culprit.path, culprit.line, culprit.function) == (
        "frontend/src/routes/items.tsx", 88, "ItemsTable",
    )  # fmt: skip
    firefox = parse_ticket(FIREFOX, paths=REPO)
    assert [(f.path, f.line) for f in firefox.frames if f.in_repo] == [
        ("frontend/src/routes/items.tsx", 40),
        ("frontend/src/client/core/request.ts", 120),
    ]


def test_an_issue_body_uses_its_fenced_trace_and_its_own_title() -> None:
    body = (
        "Creating an item without logging in crashes the API.\n\n"
        "Steps: POST /api/v1/items/ with no owner.\n\n"
        f"```\n{PYTHON}```\n\nRunning FastAPI 0.115.6 on Python 3.12.4."
    )
    ticket = parse_ticket(body, title="500 when creating an item", paths=REPO)
    assert ticket.title == "500 when creating an item"
    assert ticket.error_type == "sqlalchemy.exc.IntegrityError"
    assert ticket.versions == {"python": "3.12", "fastapi": "0.115.6"}


def test_text_without_a_trace_is_still_a_ticket() -> None:
    ticket = parse_ticket("The totals on the invoice page are off by a cent.\nSince Monday.")
    assert ticket.title == "The totals on the invoice page are off by a cent."
    assert (ticket.language, ticket.frames, ticket.error_type) == (None, [], None)


def test_paths_match_by_their_longest_shared_suffix_and_never_ambiguously() -> None:
    index = PathIndex(REPO)
    assert index.match("/home/ci/work/backend/app/crud.py") == "backend/app/crud.py"
    assert index.match("C:\\build\\backend\\app\\models.py") == "backend/app/models.py"
    assert index.match("/x/src/utils.ts") is None  # web/ or admin/: can't tell
    assert index.match("/x/web/src/utils.ts") == "web/src/utils.ts"
    assert index.match("/x/nothing.py") is None
