"""A trace's return leg (3.22): what the client does with a response (it stores a token, goes
to a page, sets its state, shows an error), found by the links stage's outcome scanner and
appended by ``trace_feature`` after a path's deepest hop. Small made-up React apps, linked as the
graph and links stages link them (a FastAPI backend for the login, so its path reaches a table)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.project import Definition, Project
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node, lane
from sensei_engine.graph.stage import RESOLVERS, Resolver, resolve_graph, resolver_for
from sensei_engine.graph.trace import TracePath, trace_feature
from sensei_engine.graph.writer import LINKS
from sensei_engine.ingest.languages import detect_language
from sensei_engine.link.outcomes import (
    OutcomeSite,
    outcome_key,
    outcome_kind,
    outcome_tie,
    scan_outcomes,
)
from sensei_engine.link.stage import LinkRequest, extract_request, links_language, resolve_links
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols

API = """\
import axios from "axios"

export const api = axios.create({ baseURL: "/api" })

export const accountClient = {
  login: async (email, password) => {
    const res = await api.post("/auth/login", { email, password })
    return res.data
  },
  refresh: async () => {
    const res = await api.post("/auth/refresh")
    localStorage.setItem("session_token", res.data.token)
  },
}
"""

LOGIN = """\
import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { toast } from "react-hot-toast"
import { accountClient } from "../api"

export default function LoginPage() {
  const [error, setError] = useState(null)
  const navigate = useNavigate()

  const handleSubmit = async (event) => {
    event.preventDefault()
    try {
      const session = await accountClient.login(event.target.email.value, "pw")
      localStorage.setItem("session_token", session.token)
      navigate("/home", { replace: true })
    } catch (err) {
      setError(err.message)
      toast.error("Could not sign in")
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <button type="button" onClick={() => navigate("/help")}>Help</button>
    </form>
  )
}
"""

BACKEND_MAIN = """\
from fastapi import FastAPI

from app.routes import router

app = FastAPI()
app.include_router(router)
"""

BACKEND_ROUTES = """\
from fastapi import APIRouter
from sqlmodel import Session, select

from app.models import Account

router = APIRouter()


@router.post("/auth/login")
def login(session: Session, body: dict) -> dict:
    return {"token": session.exec(select(Account)).first()}
"""

BACKEND_MODELS = """\
from sqlmodel import Field, SQLModel


class Account(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str
"""

BASKETS = """\
import { useEffect, useState } from "react"

export function BasketList() {
  const [baskets, setBaskets] = useState([])
  const [failure, setFailure] = useState(null)

  useEffect(() => {
    fetch("/api/baskets")
      .then((response) => response.json())
      .then((rows) => setBaskets(rows))
      .catch((problem) => setFailure(problem.message))
  }, [])

  return <ul>{baskets.map((b) => <li key={b.id}>{b.name}</li>)}</ul>
}
"""

PROFILE = """\
"use client"
import { useRouter } from "next/navigation"
import { useState } from "react"

export default function ProfileForm() {
  const router = useRouter()
  const [saved, setSaved] = useState(false)

  async function save(values) {
    const { data, status } = await fetch("/api/profile", {
      method: "PUT",
      body: JSON.stringify(values),
    }).then((r) => r.json())
    setSaved(data.saved)
    router.push(`/profile/${data.id}`)
  }

  return <form action={save} />
}
"""


def _link(root: Path, files: Mapping[str, str]) -> tuple[GraphSnapshot, list[OutcomeSite]]:
    """``files`` written under ``root`` and linked as the graph and links stages link them."""
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    languages = {
        path: language
        for path in files
        if (language := detect_language(path, files[path].encode()[:256])) is not None
    }
    definitions: dict[str, tuple[Definition, ...]] = {}
    facts: dict[str, dict[str, object]] = {}
    for path, language in languages.items():
        parsed = parse_file(path, files[path].encode(), language=language)
        definitions[path] = tuple(
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in extract_symbols(parsed, language)
        )
        name = resolver_for(language)
        if name is not None:
            found = cast("Resolver", RESOLVERS[name]).extract(parsed)
            if found is not None:
                facts.setdefault(name, {})[path] = found
    project = Project(frozenset(files), {}, definitions)
    code = resolve_graph(project, facts, clock=lambda: 0.0)
    linked = {
        path: extract_request(LinkRequest(str(root), path, language))
        for path, language in languages.items()
        if links_language(language)
    }
    links = resolve_links(project, linked)
    LINKS.check(links.nodes, links.edges)  # the outcomes are the LINKS layer's
    nodes: dict[str, Node] = {}
    for path, found in definitions.items():
        for d in found:
            nodes[d.symbol_key] = Node(
                d.symbol_key, d.kind, d.name, d.qualified_name, path, d.start_line, d.end_line,
                languages[path], lane(d.kind, path, None),
            )  # fmt: skip
    for n in [*code.nodes, *links.nodes]:
        nodes.setdefault(
            n.key,
            Node(
                n.key, n.kind.value, n.name, n.qualified_name or n.name, n.path, n.start_line,
                n.end_line, n.lang, lane(n.kind.value, n.path, None),
            ),
        )  # fmt: skip
    order = sorted(nodes)
    at = {key: i for i, key in enumerate(order)}
    edges = [
        (
            at[e.src],
            at[e.dst],
            Edge(
                e.type,
                e.confidence if e.confidence is not None else confidence_of(e.provenance),
                e.provenance,
                e.evidence_path,
                e.evidence_line,
            ),
        )
        for e in [*code.edges, *links.edges]
        if e.src in at and e.dst in at
    ]
    sites = [s for f in linked.values() if f.outcomes is not None for s in f.outcomes.sites]
    return GraphSnapshot([nodes[key] for key in order], edges), sites


def _back(path: TracePath) -> list[tuple[str, int | None]]:
    """The return leg: each hop's name and the line its edge is on."""
    assert path.return_index is not None
    return [
        (hop.node.name, hop.edge.evidence_line if hop.edge else None)
        for hop in path.hops[path.return_index :]
    ]


def _scan(path: str, text: str) -> list[OutcomeSite]:
    parsed = parse_file(path, text.encode(), language="tsx")
    definitions = [
        Definition(s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line)
        for s in extract_symbols(parsed, "tsx")
    ]
    return scan_outcomes(parsed, definitions).sites


# ---------------------------------------------------------------- the trace


@pytest.fixture
def login_app(tmp_path: Path) -> GraphSnapshot:
    graph, _ = _link(
        tmp_path,
        {
            "web/src/api.ts": API,
            "web/src/pages/LoginPage.tsx": LOGIN,
            "server/app/main.py": BACKEND_MAIN,
            "server/app/routes.py": BACKEND_ROUTES,
            "server/app/models.py": BACKEND_MODELS,
        },
    )
    return graph


def test_login_comes_back_to_the_stored_token_the_next_page_and_the_error(
    login_app: GraphSnapshot,
) -> None:
    """Down from the page to the account table, then back up: the service returns the data
    (nothing to show), the page's handler stores the token and goes home; its catch sets the
    error and toasts. The Help button's navigate follows no response: not on it."""
    result = trace_feature(login_app, "web/src/pages/LoginPage.tsx")
    best = result.paths[0]
    assert best.reaches_goal
    assert best.return_index is not None
    down = best.hops[: best.return_index]
    assert [hop.node.kind for hop in down][-1] == NodeKind.TABLE
    assert "client" in best.layers
    assert _back(best) == [
        ("LoginPage", 13),  # back to the call it made: await accountClient.login(…)
        ("localStorage.setItem session_token", 14),
        ("navigate /home", 15),
        ("setError", 17),
        ("toast.error", 18),
    ]
    leg = best.hops[best.return_index :]
    assert leg[0].edge_type is EdgeType.CALLS
    assert {hop.edge_type for hop in leg[1:]} == {EdgeType.PRODUCES}
    assert [hop.layer for hop in leg[1:]] == ["state", "ui", "state", "ui"]
    # The return leg is no part of the way down: its sureness and layers are the path's own.
    assert best.layers == [
        layer for layer in dict.fromkeys(h.layer for h in down) if layer != "other"
    ]
    assert all(hop.node.name != "navigate /help" for hop in best.hops)


def test_the_return_leg_keeps_the_direction(login_app: GraphSnapshot) -> None:
    """``up`` and ``both`` paths come back as well, after their deepest hop."""
    both = trace_feature(login_app, "web/src/api.ts", direction="both").paths[0]
    assert both.entry_index > 0  # reached from the page
    assert both.return_index is not None
    assert both.return_index > both.entry_index
    assert [name for name, _ in _back(both)][:2] == [
        "LoginPage",
        "localStorage.setItem session_token",
    ]
    up = trace_feature(login_app, "POST /auth/login", direction="up").paths[0]
    assert up.hops[up.entry_index].node.kind == NodeKind.API_ENDPOINT
    assert up.return_index == len(up.hops[: up.return_index]) == up.entry_index + 1
    assert _back(up)[0] == ("LoginPage", 13)


def test_a_list_sets_its_rows_from_a_promise_chain_and_its_error_from_the_catch(
    tmp_path: Path,
) -> None:
    graph, sites = _link(tmp_path, {"web/src/BasketList.tsx": BASKETS})
    best = trace_feature(graph, "BasketList").paths[0]
    assert not best.reaches_goal  # no backend: the call matches no endpoint
    assert _back(best) == [("BasketList", 8), ("setBaskets", 10), ("setFailure", 11)]
    by_action = {s.action: s for s in sites}
    assert by_action["setBaskets"].on_error is False
    assert by_action["setBaskets"].after == 8  # rows came from the call's chain
    assert by_action["setFailure"].on_error is True


def test_next_router_push_and_a_destructured_result(tmp_path: Path) -> None:
    graph, sites = _link(tmp_path, {"web/app/profile/ProfileForm.tsx": PROFILE})
    best = trace_feature(graph, "ProfileForm").paths[0]
    assert _back(best) == [
        ("ProfileForm", 10),  # its save() made the call: a nested function's are its own
        ("setSaved", 14),
        ("router.push /profile/${data.id}", 15),
    ]
    push = next(s for s in sites if s.action == "router.push")
    assert (push.kind, push.target, push.provenance) == (
        "navigate", "/profile/${data.id}", Provenance.RECEIVER_HEURISTIC,
    )  # fmt: skip
    saved = next(s for s in sites if s.action == "setSaved")
    assert saved.value == "data.saved"
    assert saved.after == 10  # `data` is destructured from what the await gave


def test_an_outcome_node_says_what_it_is(tmp_path: Path) -> None:
    """The links stage's facts: a ``client_outcome`` node per outcome, its attrs, and
    ``PRODUCES`` from the function on its line; ``after_call`` names the HTTP call whose result
    flows into it."""
    for path, text in {"web/src/api.ts": API, "web/src/pages/LoginPage.tsx": LOGIN}.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    files = {
        path: extract_request(
            LinkRequest(str(tmp_path), path, "tsx" if "tsx" in path else "typescript")
        )
        for path in ("web/src/api.ts", "web/src/pages/LoginPage.tsx")
    }
    definitions = {
        path: tuple(
            Definition(s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line,
                       s.end_line)
            for s in extract_symbols(
                parse_file(path, (tmp_path / path).read_bytes(), language="tsx"), "tsx"
            )
        )
        for path in files
    }  # fmt: skip
    build = resolve_links(Project(frozenset(files), {}, definitions), files)
    outcomes = {
        n.name: n
        for n in build.nodes
        if n.kind is NodeKind.CLIENT_OUTCOME and n.path == "web/src/pages/LoginPage.tsx"
    }
    token = outcomes["localStorage.setItem session_token"]
    assert dict(token.attrs) == {
        "kind": "storage",
        "action": "localStorage.setItem",
        "target": "session_token",
        "value": "session.token",
        "tie": [[13, 13]],
        "awaits": "accountClient.login",
    }
    assert outcome_tie(token.key) == ((13, 13),)
    assert outcome_kind(token.key) == "storage"
    assert outcomes["setError"].attrs["on"] == "error"
    produced = [e for e in build.edges if e.type is EdgeType.PRODUCES and e.dst == token.key]
    assert [(e.src, e.evidence_path, e.evidence_line, e.provenance) for e in produced] == [
        (
            "tsx web/src/pages/LoginPage.tsx/LoginPage().handleSubmit().",
            "web/src/pages/LoginPage.tsx",
            14,
            Provenance.CONTRACT,
        )
    ]
    # The service's refresh stores what the HTTP call itself gave: the outcome names the call.
    refreshed = next(
        n for n in build.nodes if n.kind is NodeKind.CLIENT_OUTCOME and n.path == ("web/src/api.ts")
    )
    assert refreshed.attrs["after_call"] == "http_client_call POST /auth/refresh web/src/api.ts:11"
    assert build.details["outcomes"]["after_call"] == 1


# ---------------------------------------------------------------- the scanner


def test_what_follows_no_response_is_left_out() -> None:
    """A button's navigate, a setter run on input, ``setTimeout``: no outcome of a response."""
    sites = _scan(
        "web/src/Form.tsx",
        """\
import { useState } from "react"
import { useNavigate } from "react-router-dom"

export function Form() {
  const [name, setName] = useState("")
  const navigate = useNavigate()
  const leave = () => navigate("/away")
  setTimeout(() => setName(""), 10)
  return <input onChange={(e) => setName(e.target.value)} onBlur={leave} />
}
""",
    )
    assert sites == []


def test_a_mutation_ties_its_callbacks_to_the_function_it_runs() -> None:
    """react-query's onSuccess/onError beside a local mutationFn are tied to its lines;
    ``onError: handleError`` is the error's outcome itself; ``mutate(x, {onSuccess})`` is tied
    to the hook ``mutate`` came from."""
    sites = _scan(
        "web/src/useSignup.ts",
        """\
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

export function useSignup() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const register = async (form) => {
    const created = await client.register(form)
    sessionStorage.setItem("new_member", created.id)
  }
  return useMutation({
    mutationFn: register,
    onSuccess: () => navigate({ to: "/welcome" }),
    onError: handleError,
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["members"] }),
  })
}

export function SignupButton() {
  const { mutate } = useSignup()
  const [note, setNote] = useState("")
  return <button onClick={() => mutate({}, { onSuccess: (r) => setNote(r.text) })} />
}
""",
    )
    found = {(s.action, s.target): s for s in sites}
    assert found[("sessionStorage.setItem", "new_member")].ties == ((8, 8),)
    assert found[("navigate", "/welcome")].ties == ((7, 10),)  # register's lines
    error = found[("handleError", "handleError")]
    assert (error.kind, error.on_error, error.ties) == ("error", True, ((7, 10),))
    assert found[("queryClient.invalidateQueries", '["members"]')].kind == "state"
    note = found[("setNote", "setNote")]
    assert note.ties == ((22, 22), (20, 20))  # the mutate call, then the hook it came from
    assert note.after == 22


def test_keys_carry_their_ties() -> None:
    key = outcome_key("navigate", "/x", "web/a b.tsx", 9, ((4, 6), (2, 2)))
    assert key == "client_outcome navigate /x web/a b.tsx:9 after 4-6,2-2"
    assert outcome_tie(key) == ((4, 6), (2, 2))
    assert outcome_kind(key) == "navigate"
    assert outcome_tie("http_client_call GET /x a.ts:1") is None
